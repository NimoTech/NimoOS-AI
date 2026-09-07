"""web/feed.py::feed_digest and its wiring into web/fetch.py::fetch_page.

Found by running the real thing: a scheduled "competitor radar" task fetched
three RSS/Atom feeds, each came back as ~29 KB of raw XML, got offloaded to a
file (too big for the tool result), and the model then spent most of its
turns paging that file with search_content/read_file_lines instead of just
reading three headlines — 30-minute timeout, zero report. A feed is
structured data: the model only needs title / link / date / short summary
per entry, not the raw markup.
"""
from __future__ import annotations

import httpx
import pytest

from web import fetch as wfetch
from web.feed import _MAX_FEED_BYTES, _PROLOG_SCAN_WINDOW, feed_digest

_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>GEEKOM Blog</title>
<item>
  <title>IT15 review roundup</title>
  <link>https://www.geekompc.com/blog/it15</link>
  <pubDate>Mon, 18 Aug 2026 02:30:00 GMT</pubDate>
  <description>&lt;p&gt;What &lt;b&gt;reviewers&lt;/b&gt; said.&lt;/p&gt;</description>
</item>
<item>
  <title>Mini PC restock</title>
  <link>https://www.geekompc.com/blog/restock</link>
  <pubDate>Tue, 19 Aug 2026 02:30:00 GMT</pubDate>
  <description>Back in stock notice.</description>
</item>
<item>
  <title>New firmware 1.4</title>
  <link>https://www.geekompc.com/blog/fw14</link>
  <pubDate>Wed, 20 Aug 2026 02:30:00 GMT</pubDate>
  <description>Bug fixes and stability improvements.</description>
</item>
</channel></rss>"""

_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Minisforum News</title>
<entry>
  <title>MS-A2 launches</title>
  <link rel="self" href="https://minisforum.com/feeds/news/ms-a2"/>
  <link rel="alternate" href="https://minisforum.com/blogs/news/ms-a2"/>
  <updated>2026-08-18T11:40:00Z</updated>
  <summary>The MS-A2 is available now.</summary>
</entry>
<entry>
  <title>UM890 Pro discount</title>
  <link rel="alternate" href="https://minisforum.com/blogs/news/um890-pro"/>
  <updated>2026-08-19T09:00:00Z</updated>
  <content>Discounted for a limited time.</content>
</entry>
</feed>"""


def test_rss_entries_come_back_numbered_with_tags_stripped():
    out = feed_digest(_RSS)
    assert out is not None
    assert "Feed: GEEKOM Blog — 3 entries (showing 3)" in out
    assert "1. IT15 review roundup" in out
    assert "2. Mini PC restock" in out
    assert "3. New firmware 1.4" in out
    assert "<b>" not in out and "<p>" not in out
    assert "reviewers said" in out
    assert "Mon, 18 Aug 2026" in out
    assert "link: https://www.geekompc.com/blog/it15" in out


def test_atom_entries_resolve_alternate_link_and_use_updated():
    out = feed_digest(_ATOM)
    assert out is not None
    assert "Feed: Minisforum News — 2 entries (showing 2)" in out
    assert "1. MS-A2 launches" in out
    assert "link: https://minisforum.com/blogs/news/ms-a2" in out
    assert "date: 2026-08-18T11:40:00Z" in out
    assert "summary: The MS-A2 is available now." in out
    assert "2. UM890 Pro discount" in out
    assert "link: https://minisforum.com/blogs/news/um890-pro" in out
    assert "summary: Discounted for a limited time." in out


def test_malformed_xml_returns_none():
    assert feed_digest("<rss><channel><item><title>oops") is None
    assert feed_digest("not xml at all") is None
    assert feed_digest("") is None


def test_empty_channel_returns_none():
    assert feed_digest("<rss version=\"2.0\"><channel><title>Empty</title>"
                       "</channel></rss>") is None
    assert feed_digest("<feed xmlns=\"http://www.w3.org/2005/Atom\">"
                       "<title>Empty</title></feed>") is None


def test_max_entries_is_respected_but_header_reports_the_real_total():
    items = "".join(
        f"<item><title>Item {i}</title><link>https://x.test/{i}</link>"
        f"<pubDate>2026-08-{(i % 28) + 1:02d}T00:00:00Z</pubDate>"
        f"<description>d{i}</description></item>"
        for i in range(50)
    )
    body = f"<rss version=\"2.0\"><channel><title>Big</title>{items}</channel></rss>"
    out = feed_digest(body)
    assert out is not None
    assert "Feed: Big — 50 entries (showing 40)" in out
    assert "1. Item 0" in out
    assert "40. Item 39" in out
    assert "41. Item 40" not in out
    assert "Item 49" not in out


def test_summary_is_truncated_to_300_chars_with_an_ellipsis():
    long_summary = "word " * 100  # 500 chars
    body = (f"<rss version=\"2.0\"><channel><title>T</title>"
            f"<item><title>x</title><link>https://x.test</link>"
            f"<description>{long_summary}</description></item>"
            f"</channel></rss>")
    out = feed_digest(body)
    assert out is not None
    lines = [l for l in out.splitlines() if l.strip().startswith("summary:")]
    assert len(lines) == 1
    summary_text = lines[0].split("summary:", 1)[1].strip()
    assert len(summary_text) <= 300
    assert summary_text.endswith("…")


def test_rdf_rss10_items_are_read():
    body = (
        '<?xml version="1.0"?>'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        'xmlns="http://purl.org/rss/1.0/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<channel><title>Old Skool</title></channel>'
        '<item><title>Entry A</title><link>https://x.test/a</link>'
        '<dc:date>2026-08-01</dc:date>'
        '<description>About A.</description></item>'
        '</rdf:RDF>'
    )
    out = feed_digest(body)
    assert out is not None
    assert "Feed: Old Skool — 1 entry (showing 1)" in out
    assert "1. Entry A" in out
    assert "date: 2026-08-01" in out


def test_rss_with_default_namespace_on_root_still_digests():
    # Non-standard but seen in the wild: a default xmlns on <rss> itself,
    # which breaks a plain (namespace-blind) root.find("channel")/
    # findall("item").
    body = ('<?xml version="1.0"?>'
            '<rss version="2.0" xmlns="http://purl.org/rss/1.0/">'
            '<channel><title>NSFeed</title>'
            '<item><title>Only item</title><link>https://x.test/only</link>'
            '<pubDate>2026-01-01</pubDate><description>d</description></item>'
            '</channel></rss>')
    out = feed_digest(body)
    assert out is not None
    assert "Feed: NSFeed — 1 entry (showing 1)" in out
    assert "1. Only item" in out


def test_doctype_inside_a_cdata_description_does_not_block_the_digest():
    # A feed's <description> legitimately quoting an HTML snippet via CDATA
    # is content, not a prolog DTD — it comes long after the root's own
    # start tag and must not be mistaken for one.
    body = ('<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
            '<item><title>x</title><link>https://x.test</link>'
            '<description><![CDATA[<!DOCTYPE html><p>hi</p>]]></description>'
            '</item></channel></rss>')
    out = feed_digest(body)
    assert out is not None
    assert "1. x" in out


def test_real_doctype_prolog_is_refused():
    # A DOCTYPE (with an internal-subset ENTITY, the billion-laughs shape)
    # declared where the XML grammar actually allows one — before the root
    # element's start tag — must still be refused.
    body = ('<?xml version="1.0"?>'
            '<!DOCTYPE rss [<!ENTITY x "lol">]>'
            '<rss version="2.0"><channel><title>T</title>'
            '<item><title>x</title><link>https://x.test</link>'
            '<description>&x;</description></item></channel></rss>')
    assert feed_digest(body) is None


def test_doctype_hidden_after_a_prolog_comment_containing_a_bare_tag_is_refused():
    # Bypass found in review: a prolog comment containing "<a" made a naive
    # "first '<name'" scan stop INSIDE the comment, so a real DOCTYPE placed
    # after it was never reached and its entity expanded unchecked. Comments
    # must be stripped before the root element's start tag is located.
    body = ('<?xml version="1.0"?>'
            '<!-- a comment with <a> embedded -->'
            '<!DOCTYPE rss [<!ENTITY x "lol">]>'
            '<rss version="2.0"><channel><title>T</title>'
            '<item><title>x</title><link>https://x.test</link>'
            '<description>&x;</description></item></channel></rss>')
    assert feed_digest(body) is None


def test_prolog_comment_with_a_bare_tag_but_no_doctype_still_digests():
    # The flip side of the bypass test: a prolog comment containing "<a>" is
    # legitimate on its own and must not itself cause a false refusal.
    body = ('<?xml version="1.0"?>'
            '<!-- a comment with <a> embedded -->'
            '<rss version="2.0"><channel><title>T</title>'
            '<item><title>x</title><link>https://x.test</link>'
            '<description>d</description></item></channel></rss>')
    out = feed_digest(body)
    assert out is not None
    assert "1. x" in out


def test_prolog_padded_with_whitespace_past_the_scan_window_is_refused():
    # Bypass found in review: pad the prolog with enough junk that no root
    # start tag falls inside the bounded scan window at all. Round 2 made
    # "no root tag found" fall back to treating the whole window as an
    # (apparently harmless) prolog — which let a DOCTYPE/ENTITY placed right
    # after the window boundary reach ET.fromstring() completely unexamined.
    prefix = '<?xml version="1.0"?>'
    head_part = prefix + " " * (_PROLOG_SCAN_WINDOW - len(prefix))
    assert len(head_part) == _PROLOG_SCAN_WINDOW
    body = (head_part +
            '<!DOCTYPE rss [<!ENTITY x "lol">]>'
            '<rss version="2.0"><channel><title>T</title>'
            '<item><title>x</title><link>https://x.test</link>'
            '<description>&x;</description></item></channel></rss>')
    assert feed_digest(body) is None


def test_prolog_padded_with_a_wellformed_comment_past_the_scan_window_is_refused():
    # Same bypass shape as above, but the padding itself is a legitimate,
    # fully-closed comment (not just whitespace) — it must strip cleanly and
    # STILL be refused once no root tag is left inside the window, rather
    # than the successful strip being mistaken for "nothing to worry about".
    prefix = '<?xml version="1.0"?>'
    comment = "<!--" + ("x" * 60000) + "-->"
    before = prefix + comment
    head_part = before + " " * (_PROLOG_SCAN_WINDOW - len(before))
    assert len(head_part) == _PROLOG_SCAN_WINDOW
    body = (head_part +
            '<!DOCTYPE rss [<!ENTITY x "lol">]>'
            '<rss version="2.0"><channel><title>T</title>'
            '<item><title>x</title><link>https://x.test</link>'
            '<description>&x;</description></item></channel></rss>')
    assert feed_digest(body) is None


def test_normal_feed_with_a_1kb_comment_prolog_still_digests():
    # No false positive: a real (short-ish) prolog comment, well within the
    # scan window, must not itself trigger the "no root found" refusal.
    comment = "<!--" + ("y" * 1000) + "-->"
    body = ('<?xml version="1.0"?>' + comment +
            '<rss version="2.0"><channel><title>T</title>'
            '<item><title>x</title><link>https://x.test</link>'
            '<description>d</description></item></channel></rss>')
    out = feed_digest(body)
    assert out is not None
    assert "1. x" in out


def test_unterminated_prolog_comment_hiding_a_doctype_is_refused():
    # An unterminated "<!--" is left in place rather than swallowing the
    # rest of the scan window — it must fail safe (refuse), not accidentally
    # let the DOCTYPE/ENTITY behind it slip through unexamined.
    body = ('<?xml version="1.0"?>'
            '<!-- unterminated comment '
            '<!DOCTYPE rss [<!ENTITY x "lol">]>'
            '<rss version="2.0"><channel><title>T</title>'
            '<item><title>x</title><link>https://x.test</link>'
            '<description>&x;</description></item></channel></rss>')
    assert feed_digest(body) is None


def test_oversized_body_returns_none_before_parsing():
    huge_desc = "x" * (_MAX_FEED_BYTES + 1000)
    body = (f'<rss version="2.0"><channel><title>T</title>'
            f'<item><title>i</title><link>https://x.test</link>'
            f'<description>{huge_desc}</description></item></channel></rss>')
    assert feed_digest(body) is None


def test_title_link_date_are_whitespace_collapsed_and_capped():
    # A newline inside a title must not be able to forge what looks like an
    # extra numbered entry line in the rendered digest.
    injected_title = "Real Title\n2. Fake Entry\n   link: https://evil.test"
    huge_link = "https://x.test/" + ("a" * 3000)
    body = (f'<rss version="2.0"><channel><title>T</title>'
            f'<item><title>{injected_title}</title>'
            f'<link>{huge_link}</link>'
            f'<pubDate>{"2026-01-01 " * 20}</pubDate>'
            f'<description>d</description></item></channel></rss>')
    out = feed_digest(body)
    assert out is not None
    lines = out.splitlines()
    assert "2. Fake Entry" not in lines
    assert any(l.startswith("1. Real Title 2. Fake Entry") for l in lines)
    link_line = next(l for l in lines if l.strip().startswith("link:"))
    assert len(link_line.split("link:", 1)[1].strip()) <= 2048
    date_line = next(l for l in lines if l.strip().startswith("date:"))
    assert len(date_line.split("date:", 1)[1].strip()) <= 64


# --- fetch_page-level wiring -------------------------------------------

def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _clean_cache():
    wfetch.clear_cache()
    yield
    wfetch.clear_cache()


@pytest.mark.asyncio
async def test_fetch_page_returns_a_digest_for_an_rss_response():
    async def handler(request):
        return httpx.Response(200, content=_RSS.encode("utf-8"),
                              headers={"Content-Type": "application/rss+xml"})

    async with _client(handler) as c:
        out = await wfetch.fetch_page("https://www.geekompc.com/blog/feed/", client=c)

    assert out.get("kind") == "feed"
    assert out.get("entries") == 3
    assert out["content_markdown"].startswith("Feed:")
    assert "IT15 review roundup" in out["content_markdown"]
    assert out["title"] == "GEEKOM Blog"


@pytest.mark.asyncio
async def test_fetch_page_normal_html_is_unaffected():
    async def handler(request):
        return httpx.Response(200, html="<html><head><title>T</title></head>"
                                        "<body><p>hello body</p></body></html>")

    async with _client(handler) as c:
        out = await wfetch.fetch_page("https://x.test/a", client=c)

    assert "kind" not in out
    assert "entries" not in out
    assert out["title"] == "T"
    assert "hello body" in out["content_markdown"]
