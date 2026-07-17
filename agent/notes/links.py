"""Link extraction for note bodies: standard markdown links (system-emitted,
OKF-compliant) plus human-written [[wikilinks]]. Tolerant classifier —
broken/dangling refs are stored as-is (OKF: consumers MUST tolerate)."""
from __future__ import annotations

import re

_MD_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")


def _classify(ref: str) -> str:
    low = ref.lower()
    if low.startswith(("http://", "https://")):
        return "url"
    if low.endswith(".md"):
        return "note"
    if ref.startswith("/"):
        return "file"
    return "note"


def extract_links(body: str) -> list[dict]:
    out, seen = [], set()
    for m in _MD_LINK.finditer(body):
        anchor, ref = m.group(1), m.group(2)
        key = (_classify(ref), ref)
        if key not in seen:
            seen.add(key)
            out.append({"dst_kind": key[0], "dst_ref": ref,
                        "anchor_text": anchor})
    for m in _WIKILINK.finditer(body):
        ref = m.group(1).strip()
        key = ("note", ref)
        if key not in seen:
            seen.add(key)
            out.append({"dst_kind": "note", "dst_ref": ref,
                        "anchor_text": ref})
    return out
