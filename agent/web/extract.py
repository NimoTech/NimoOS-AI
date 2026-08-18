"""HTML → text, trafilatura first with a zero-dependency backstop.

Two levels, deliberately (spec §2.2): trafilatura already carries its own
recall fallback, and the LAST level must have no dependencies at all — a
backstop that can itself fail to import is not a backstop. The result of
this module is never None and never an exception: web_fetch must always be
able to return something the model can read, or a clear error, never a
traceback.

The backstop drops the *contents* of only `script` and `style`. Those two
are the "raw text" elements per `html.parser.HTMLParser` (its
`CDATA_CONTENT_ELEMENTS`): the parser hands their body over verbatim and
only leaves that mode on a real matching close tag, so an unclosed
`<script>`/`<style>` swallows the remainder of the document exactly like a
browser would — that content would not have rendered either, and it is
exactly the payload (JS source, CSS) that must never reach the model.
`noscript`, `template` and `svg` get ordinary parsing, so nothing but a
manual counter would keep their bodies suppressed — and an unclosed one
(this backstop runs on arbitrary internet HTML that `web_fetch` may itself
have truncated mid-document at its size cap, so unbalanced tags are
expected input, not an edge case) would poison that counter and silently
discard everything after it. Their text is inert (fallback copy, unrendered
template markup, SVG labels), so it is left in rather than risk that.
"""
from __future__ import annotations

import re
from html import unescape as _unescape
from html.parser import HTMLParser

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\f\v]+")
_BLANKS_RE = re.compile(r"\n{3,}")

# Raw-text (CDATA) elements only — see module docstring for why the set stops here.
_RAWTEXT_SKIP_TAGS = {"script", "style"}
_BREAK_TAGS = {"p", "div", "br", "li", "tr", "section", "article",
               "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}


def title_of(html: str) -> str:
    m = _TITLE_RE.search(html or "")
    if not m:
        return ""
    text = _TAG_RE.sub("", m.group(1))
    text = _unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _try_trafilatura(html: str, url: str) -> str:
    """Primary extractor. Returns "" on any failure, including ImportError.

    Imported lazily: the module must stay importable (and its backstop
    testable) on a machine where the wheel is missing.
    """
    try:
        import trafilatura  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — a missing/broken wheel is a fallback, not an error
        return ""
    try:
        out = trafilatura.extract(
            html,
            url=url or None,
            output_format="markdown",
            include_links=True,
            include_tables=True,
            with_metadata=False,
        )
    except Exception:  # noqa: BLE001 — extractor crash → fall through to backstop
        return ""
    return (out or "").strip()


class _TextCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        # Name of the raw-text tag currently being skipped, or None. A plain
        # flag (not a depth counter) is enough: CDATA_CONTENT_ELEMENTS mode
        # means script/style cannot nest, and a stray close tag while not
        # skipping is simply ignored (falsy guard below), never going negative.
        self._skipping: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag in _RAWTEXT_SKIP_TAGS:
            self._skipping = tag
            return
        if tag in _BREAK_TAGS:
            self._out.append("\n")

    def handle_endtag(self, tag):
        if self._skipping and tag == self._skipping:
            self._skipping = None
            return
        if tag in _BREAK_TAGS:
            self._out.append("\n")

    def handle_data(self, data):
        if not self._skipping:
            self._out.append(data)

    def text(self) -> str:
        return "".join(self._out)


def _strip_tags(html: str) -> str:
    p = _TextCollector()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 — malformed HTML must not raise out of here
        pass
    text = _WS_RE.sub(" ", p.text())
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANKS_RE.sub("\n\n", text).strip()


def to_markdown(html: str, *, url: str = "") -> str:
    if not html or not html.strip():
        return ""
    md = _try_trafilatura(html, url)
    if md:
        return md
    return _strip_tags(html)
