"""web_fetch must be able to read an RSS/Atom feed.

Found by running the real thing. A scheduled task told to check three competitor
blogs reported all three as failures — "Atom feed 不支持", "RSS 不支持" — and
fell back to scraping the HTML listing pages instead. The cause was the
content-type gate: `_OK_TYPES` listed `text/xml` and `application/xml` but not
`application/rss+xml` or `application/atom+xml`, which is exactly what every
real feed serves (Shopify's `.atom`, WordPress's `/feed/`, frame.work's
`blog.rss`).

Why that gap mattered more than it looks: a feed is the only source in this
whole exercise that is EXACT. Search results carry a provider's approximate
`published` value and no guarantee of completeness; a feed carries real
timestamps, real titles and the publisher's own summary, with no API key. With
feeds unreadable, an agent-loop digest has to infer recency from search
snippets — which is how a six-day-old video ends up in a 72-hour report.

The `Accept` header is widened for the same reason: a server that content-
negotiates would hand back HTML for a URL the caller asked for precisely
because it is a feed.
"""
from __future__ import annotations

import httpx
import pytest

from web import fetch as wfetch


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


_ATOM = (b'<?xml version="1.0" encoding="UTF-8"?>'
         b'<feed xmlns="http://www.w3.org/2005/Atom">'
         b'<title>Minisforum News</title>'
         b'<entry><title>MS-A2 launches</title>'
         b'<link href="https://minisforum.com/blogs/news/ms-a2"/>'
         b'<updated>2026-08-18T11:40:00Z</updated>'
         b'<summary>The MS-A2 is available now.</summary></entry>'
         b'</feed>')

_RSS = (b'<?xml version="1.0"?><rss version="2.0"><channel>'
        b'<title>GEEKOM Blog</title>'
        b'<item><title>IT15 review roundup</title>'
        b'<link>https://www.geekompc.com/blog/it15</link>'
        b'<pubDate>Mon, 18 Aug 2026 02:30:00 GMT</pubDate>'
        b'<description>What reviewers said.</description></item>'
        b'</channel></rss>')


@pytest.mark.parametrize("ctype", [
    "application/atom+xml",
    "application/atom+xml; charset=utf-8",
    "application/rss+xml",
    "application/rss+xml; charset=UTF-8",
    "text/xml",
    "application/xml",
])
def test_every_content_type_a_real_feed_serves_is_accepted(ctype):
    assert wfetch._content_type_ok(ctype), ctype


@pytest.mark.parametrize("ctype", [
    "application/pdf",
    "image/png",
    "application/octet-stream",
    "application/zip",
])
def test_binary_types_are_still_refused(ctype):
    # The gate still exists: this tool reads pages, not documents.
    assert not wfetch._content_type_ok(ctype), ctype


@pytest.mark.asyncio
async def test_an_atom_feed_comes_back_with_its_entries_readable():
    async def handler(request):
        return httpx.Response(200, content=_ATOM,
                              headers={"Content-Type": "application/atom+xml"})

    async with _client(handler) as c:
        got = await wfetch._fetch_once(c, "https://minisforum.com/blogs/news.atom")

    assert "error" not in got, got
    body = got["_body"]
    assert "MS-A2 launches" in body
    assert "2026-08-18T11:40:00Z" in body


@pytest.mark.asyncio
async def test_an_rss_feed_comes_back_with_its_items_readable():
    async def handler(request):
        return httpx.Response(200, content=_RSS,
                              headers={"Content-Type": "application/rss+xml"})

    async with _client(handler) as c:
        got = await wfetch._fetch_once(c, "https://www.geekompc.com/blog/feed/")

    assert "error" not in got, got
    assert "IT15 review roundup" in got["_body"]
    assert "18 Aug 2026" in got["_body"]


@pytest.mark.asyncio
async def test_the_request_advertises_that_it_will_take_a_feed():
    # Otherwise a content-negotiating server answers a feed URL with HTML.
    seen = {}

    async def handler(request):
        seen["accept"] = request.headers.get("Accept", "")
        return httpx.Response(200, content=_RSS,
                              headers={"Content-Type": "application/rss+xml"})

    async with _client(handler) as c:
        await wfetch._fetch_once(c, "https://example.test/feed")

    assert "rss+xml" in seen["accept"] or "atom+xml" in seen["accept"], seen


@pytest.mark.asyncio
async def test_an_unsupported_type_still_says_so_without_echoing_the_header():
    # The header is remote text that lands in the model's context; the refusal
    # must not interpolate it.
    async def handler(request):
        return httpx.Response(200, content=b"%PDF-1.4",
                              headers={"Content-Type": "application/pdf; x=<inject>"})

    async with _client(handler) as c:
        got = await wfetch._fetch_once(c, "https://example.test/x.pdf")

    assert "error" in got
    assert "inject" not in got["error"]
    assert "read_document" in got["error"]
