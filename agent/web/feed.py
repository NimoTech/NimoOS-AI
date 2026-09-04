"""RSS/Atom/RDF feed body -> a compact per-entry digest.

A feed is structured data, not prose: fetching it whole and handing the model
~29 KB of raw XML forces it to page through markup with `search_content` /
`read_file_lines` to find title / link / date / summary per entry — which is
exactly what burned a 30-minute scheduled-task run (see
web/fetch.py's feed-detection comment). Parsing it here once, server-side,
turns that into a handful of short lines the model can read directly.

`xml.etree.ElementTree` is used deliberately over a full feedparser dependency
— feed structure (RSS 2.0 / Atom / RSS 1.0-RDF) is small and stable enough
that a hand-rolled reader is a few dozen lines, and it keeps this module
dependency-free like web/extract.py's backstop.

Never raises: malformed or unrecognised XML, an oversized body, or a feed
with zero entries, all come back as None. A feed digest that crashed instead
of falling back would turn "unsupported feed" into "web_fetch is broken".
"""
from __future__ import annotations

import re
from xml.etree import ElementTree as ET

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")

# Parsing/memory gate, checked before ElementTree ever sees the body. Real
# feeds this digest exists for (blog/news RSS, Atom) are tens of KB; a body
# past this is either not really a "feed to skim" or hostile, and either way
# is cheaper to refuse than to hand to the XML parser.
_MAX_FEED_BYTES = 2 * 1024 * 1024  # 2 MiB

_TITLE_MAX = 200
_DATE_MAX = 64
_LINK_MAX = 2048

# stdlib ElementTree does not resolve external entities (no XXE), but it does
# still expand internally-declared ones — a remote feed is exactly the kind
# of untrusted input a "billion laughs" DOCTYPE/ENTITY payload targets, and
# no real RSS/Atom/RDF feed needs a DTD. Refusing anything that declares one
# is cheaper and dependency-free compared to pulling in defusedxml for a
# reader that only ever sees feed markup.
#
# Scoped to the PROLOG only (the text before the root element's own start
# tag) rather than the whole body: a DOCTYPE can only be declared there per
# the XML grammar, but the literal string "<!DOCTYPE" shows up completely
# legitimately deep inside a feed — e.g. a <description> that CDATA-quotes
# an HTML snippet ("<![CDATA[<!DOCTYPE html>...]]>"). Scanning the whole body
# for that marker refused exactly the content-rich feeds this digest exists
# to read.
_DTD_MARKER_RE = re.compile(r"<!(?:doctype|entity)\b", re.IGNORECASE)
# First "real" element start tag: a '<' not immediately followed by '?' (a
# processing instruction, including the XML declaration) or '!' (a comment
# or the DOCTYPE itself, including any internal-subset "<!ENTITY ...>"
# declarations nested inside it — those also start with '<!' so this
# doesn't stop short partway through a DOCTYPE's own brackets).
_ROOT_START_RE = re.compile(r"<(?!\?|!)[A-Za-z_:]")


def _prolog(body: str) -> str:
    """Everything before the root element's opening tag."""
    m = _ROOT_START_RE.search(body)
    return body[: m.start()] if m else body


def _local(tag: str) -> str:
    """Strip a `{namespace}` prefix: '{http://...}item' -> 'item'.

    Real feeds are inconsistent about which elements carry which namespace
    (a default xmlns on the root is common even on nominally-unnamespaced
    RSS 2.0), so matching by local name only — for the root tag AND every
    child lookup below — is what actually works across samples in the wild.
    """
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _strip_html(text: str) -> str:
    """Best-effort HTML stripping for <description>/<summary> bodies.

    Reuses web/extract.py's zero-dependency tag stripper when importable
    (avoids a second regex-based implementation drifting from the first);
    falls back to a plain regex if that import ever fails for any reason —
    a broken summary must never take the whole digest down with it.
    """
    if not text:
        return ""
    try:
        from web.extract import _strip_tags  # noqa: PLC0415
        return _strip_tags(text)
    except Exception:  # noqa: BLE001
        return _TAG_RE.sub("", text)


def _collapse_ws(text: str) -> str:
    """Collapse all whitespace (newlines included) to single spaces.

    Applied to every rendered field, not just summary: the digest's entries
    are numbered plain-text lines ("N. title" / "   link: ..."), so a title
    or date containing a raw newline could otherwise forge what looks like
    an extra numbered entry or field line in the model's context.
    """
    return _WS_RE.sub(" ", text or "").strip()


def _clean_summary(text: str, limit: int) -> str:
    collapsed = _collapse_ws(_strip_html(text))
    if len(collapsed) <= limit:
        return collapsed
    if limit <= 0:
        return ""
    return collapsed[: limit - 1] + "…"  # ellipsis counts toward the cap


def _clean_title(text: str) -> str:
    return _collapse_ws(text)[:_TITLE_MAX]


def _clean_date(text: str) -> str:
    return _collapse_ws(text)[:_DATE_MAX]


def _clean_link(text: str) -> str:
    return _collapse_ws(text)[:_LINK_MAX]


def _child_local(el: ET.Element, name: str) -> str:
    """First direct child whose local tag name matches, text only."""
    for child in el:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _rss_entries(root: ET.Element) -> tuple[str, list[dict]]:
    channel = None
    for child in root:
        if _local(child.tag) == "channel":
            channel = child
            break
    if channel is None:
        return "", []
    title = _clean_title(_child_local(channel, "title"))
    entries = [
        {
            "title": _clean_title(_child_local(item, "title")),
            "link": _clean_link(_child_local(item, "link")),
            "date": _clean_date(_child_local(item, "pubDate")),
            "summary": _child_local(item, "description"),
        }
        for item in channel if _local(item.tag) == "item"
    ]
    return title, entries


def _atom_entries(root: ET.Element) -> tuple[str, list[dict]]:
    title = _clean_title(_child_local(root, "title"))
    entries = []
    for entry in root:
        if _local(entry.tag) != "entry":
            continue
        first_href, alt_href = "", ""
        for link_el in entry:
            if _local(link_el.tag) != "link":
                continue
            href = link_el.get("href", "")
            if not href:
                continue
            if not first_href:
                first_href = href
            # No rel="..." at all defaults to "alternate" per the Atom spec.
            if link_el.get("rel", "alternate") == "alternate":
                alt_href = href
                break
        entries.append({
            "title": _clean_title(_child_local(entry, "title")),
            "link": _clean_link(alt_href or first_href),
            "date": _clean_date(_child_local(entry, "updated")
                                or _child_local(entry, "published")),
            "summary": _child_local(entry, "summary") or _child_local(entry, "content"),
        })
    return title, entries


def _rdf_entries(root: ET.Element) -> tuple[str, list[dict]]:
    # RSS 1.0 / RDF: <item> is a sibling of <channel>, not nested inside it,
    # and both can sit at any depth depending on how the feed declares its
    # default namespace — so this walks the whole tree by local name rather
    # than assuming a fixed shape like the RSS 2.0 / Atom cases above.
    title = ""
    for el in root.iter():
        if _local(el.tag) == "channel":
            title = _clean_title(_child_local(el, "title"))
            break
    entries = [
        {
            "title": _clean_title(_child_local(item, "title")),
            "link": _clean_link(_child_local(item, "link")),
            "date": _clean_date(_child_local(item, "date")),  # dc:date, by local name
            "summary": _child_local(item, "description"),
        }
        for item in root.iter() if _local(item.tag) == "item"
    ]
    return title, entries


def _parse(body: str) -> tuple[str, int, list[dict]] | None:
    """(title, entry_count, entries) for a recognised feed root, else None.

    The single ElementTree walk both feed_digest() and fetch_page's metadata
    need — see digest_with_meta() below, which is the one place that walk
    happens for a real fetch.
    """
    if not body or not body.strip():
        return None
    if _DTD_MARKER_RE.search(_prolog(body)):
        return None
    raw = body.encode("utf-8", errors="replace")
    if len(raw) > _MAX_FEED_BYTES:
        return None
    try:
        # fromstring() rejects a `str` that itself carries an
        # `encoding="..."` XML declaration (ValueError) — nearly every real
        # feed has one. Body is already-decoded text by the time it gets
        # here (web/fetch.py's _read_capped), so parsing the re-encoded
        # bytes lets ElementTree read the declaration itself instead of
        # fighting it — and reuses the same `raw` the size gate above just
        # computed rather than encoding twice.
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError, UnicodeError):
        return None
    tag = _local(root.tag).lower()
    if tag == "rss":
        title, entries = _rss_entries(root)
    elif tag == "feed":
        title, entries = _atom_entries(root)
    elif tag == "rdf":
        title, entries = _rdf_entries(root)
    else:
        return None
    if not entries:
        return None
    return title, len(entries), entries


def digest_with_meta(
    body: str, *, max_entries: int = 40, summary_chars: int = 300
) -> tuple[str, str, int] | None:
    """(digest_text, feed_title, total_entry_count), or None.

    The parse-once entry point: fetch_page calls this exactly once per feed
    body and gets both the rendered text and the metadata it needs for the
    result dict, instead of parsing the same XML twice (once to render, once
    to count).
    """
    parsed = _parse(body)
    if parsed is None:
        return None
    title, total, entries = parsed
    shown = entries[:max_entries]
    noun = "entry" if total == 1 else "entries"
    lines = [f"Feed: {title or '(untitled feed)'} — {total} {noun} "
             f"(showing {len(shown)})"]
    for i, entry in enumerate(shown, 1):
        summary = _clean_summary(entry.get("summary", ""), summary_chars) or "-"
        lines.append(f"{i}. {entry.get('title') or '(untitled)'}")
        lines.append(f"   link: {entry.get('link') or ''}")
        lines.append(f"   date: {entry.get('date') or 'unknown'}")
        lines.append(f"   summary: {summary}")
    return "\n".join(lines), title, total


def feed_digest(body: str, *, max_entries: int = 40, summary_chars: int = 300) -> str | None:
    """A compact, deterministic text digest of *body*, or None if it isn't a
    feed ElementTree can parse or it has zero entries.

    Thin wrapper over digest_with_meta() for callers (and tests) that only
    want the rendered text, not the metadata tuple.
    """
    result = digest_with_meta(body, max_entries=max_entries, summary_chars=summary_chars)
    return result[0] if result is not None else None
