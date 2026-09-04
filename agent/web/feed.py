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

Never raises: malformed or unrecognised XML, or a feed with zero entries,
both come back as None. A feed digest that crashed instead of falling back
would turn "unsupported feed" into "web_fetch is broken".
"""
from __future__ import annotations

import re
from xml.etree import ElementTree as ET

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")

# stdlib ElementTree does not resolve external entities (no XXE), but it does
# still expand internally-declared ones — a remote feed is exactly the kind
# of untrusted input a "billion laughs" DOCTYPE/ENTITY payload targets, and
# no real RSS/Atom/RDF feed needs a DTD. Refusing anything that declares one
# is cheaper and dependency-free compared to pulling in defusedxml for a
# reader that only ever sees feed markup.
_DTD_MARKER_RE = re.compile(r"<!(?:doctype|entity)\b", re.IGNORECASE)


def _local(tag: str) -> str:
    """Strip a `{namespace}` prefix: '{http://...}item' -> 'item'.

    Real feeds are inconsistent about which elements carry which namespace
    (RSS 1.0/RDF in particular), so matching by local name only is what
    actually works across samples in the wild.
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


def _clean_summary(text: str, limit: int) -> str:
    collapsed = _WS_RE.sub(" ", _strip_html(text)).strip()
    return collapsed[:limit]


def _child_local(el: ET.Element, name: str) -> str:
    """First direct child whose local tag name matches, text only."""
    for child in el:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _rss_entries(root: ET.Element) -> tuple[str, list[dict]]:
    channel = root.find("channel")
    if channel is None:
        return "", []
    title = _child_local(channel, "title")
    entries = [
        {
            "title": _child_local(item, "title"),
            "link": _child_local(item, "link"),
            "date": _child_local(item, "pubDate"),
            "summary": _child_local(item, "description"),
        }
        for item in channel.findall("item")
    ]
    return title, entries


def _atom_entries(root: ET.Element) -> tuple[str, list[dict]]:
    title = _child_local(root, "title")
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
            "title": _child_local(entry, "title"),
            "link": alt_href or first_href,
            "date": _child_local(entry, "updated") or _child_local(entry, "published"),
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
            title = _child_local(el, "title")
            break
    entries = [
        {
            "title": _child_local(item, "title"),
            "link": _child_local(item, "link"),
            "date": _child_local(item, "date"),  # dc:date, matched by local name
            "summary": _child_local(item, "description"),
        }
        for item in root.iter() if _local(item.tag) == "item"
    ]
    return title, entries


def _parse(body: str) -> tuple[str, list[dict]] | None:
    """(title, entries) for a recognised feed root, else None."""
    if not body or not body.strip():
        return None
    if _DTD_MARKER_RE.search(body):
        return None
    try:
        # fromstring() rejects a `str` that itself carries an
        # `encoding="..."` XML declaration (ValueError) — nearly every real
        # feed has one. Body is already-decoded text by the time it gets
        # here (web/fetch.py's _read_capped), so re-encoding to bytes lets
        # ElementTree parse the declaration itself instead of fighting it.
        root = ET.fromstring(body.encode("utf-8", errors="replace"))
    except (ET.ParseError, ValueError, UnicodeError):
        return None
    tag = _local(root.tag).lower()
    if tag == "rss":
        return _rss_entries(root)
    if tag == "feed":
        return _atom_entries(root)
    if tag == "rdf":
        return _rdf_entries(root)
    return None


def feed_title_and_count(body: str) -> tuple[str, int] | None:
    """(feed title, entry count) for fetch_page's result metadata.

    A thin peek alongside feed_digest() rather than folding this into it —
    fetch_page needs the title and count as separate result-dict fields, and
    re-deriving them by parsing the formatted digest text back out would be
    both slower and one format change away from breaking silently.
    """
    parsed = _parse(body)
    if parsed is None:
        return None
    title, entries = parsed
    if not entries:
        return None
    return title, len(entries)


def feed_digest(body: str, *, max_entries: int = 40, summary_chars: int = 300) -> str | None:
    """A compact, deterministic text digest of *body*, or None if it isn't a
    feed ElementTree can parse or it has zero entries.

    One block per entry — title, link, date, summary — capped at
    *max_entries* (the header still reports the real total so the model
    knows there's more, it just isn't shown all of it).
    """
    parsed = _parse(body)
    if parsed is None:
        return None
    title, entries = parsed
    if not entries:
        return None
    shown = entries[:max_entries]
    lines = [f"Feed: {title or '(untitled feed)'} — {len(entries)} entries "
             f"(showing {len(shown)})"]
    for i, entry in enumerate(shown, 1):
        summary = _clean_summary(entry.get("summary", ""), summary_chars) or "-"
        lines.append(f"{i}. {entry.get('title') or '(untitled)'}")
        lines.append(f"   link: {entry.get('link') or ''}")
        lines.append(f"   date: {entry.get('date') or 'unknown'}")
        lines.append(f"   summary: {summary}")
    return "\n".join(lines)
