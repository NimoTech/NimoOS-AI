"""Small-document inlining shared by the nimoos_search tool and the ask pipeline.

A spec sheet, a note, a CSV is ~2-5k chars: a 200-char preview never holds the
field the user asked about, so answering used to cost a second hop. Fetching
the complete text lets the model answer from the search result directly.
Bounded three ways so a broad query cannot blow up the context: per-document
size, number of documents, total budget.
"""
from __future__ import annotations

import asyncio

INLINE_MAX_DOC_CHARS = 8000
INLINE_MAX_DOCS = 3
INLINE_TOTAL_BUDGET = 16000
_INLINE_MIME_PREFIXES = ("text/",)


def inline_eligible(mime: str, kind: str) -> bool:
    return (kind or "body") == "body" and str(mime or "").startswith(_INLINE_MIME_PREFIXES)


async def fetch_small_documents(file_ids: list[str], *, invoke_tool, user_id: str) -> dict[str, str]:
    """file_id -> complete text for the first INLINE_MAX_DOCS ids, honouring the
    per-doc cap and the total budget in the given order. Best effort: a failed
    or truncated fetch simply leaves that id out."""
    ids: list[str] = []
    for fid in file_ids:
        if fid and fid not in ids:
            ids.append(fid)
        if len(ids) >= INLINE_MAX_DOCS:
            break
    if not ids:
        return {}

    async def fetch(fid):
        try:
            return await invoke_tool("read_document", {
                "file_id": fid, "offset": 0, "max_chars": INLINE_MAX_DOC_CHARS}, user_id=user_id)
        except Exception:  # noqa: BLE001 — inlining is an optimisation only
            return None

    docs = await asyncio.gather(*(fetch(fid) for fid in ids))
    out: dict[str, str] = {}
    used = 0
    for fid, doc in zip(ids, docs):
        if not isinstance(doc, dict) or doc.get("truncated"):
            continue
        text = doc.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        if len(text) > INLINE_MAX_DOC_CHARS or used + len(text) > INLINE_TOTAL_BUDGET:
            continue
        out[fid] = text
        used += len(text)
    return out
