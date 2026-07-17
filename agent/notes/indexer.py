"""Push notes into Parser's knowledge_notes collection. Fire-and-forget
semantics: indexing failure NEVER blocks a note write — the sync scanner's
next pass retries naturally (content_hash mismatch vs Qdrant is invisible
here; we simply re-upsert on every change, which is idempotent)."""
from __future__ import annotations

import logging

_LOG = logging.getLogger("nimoos-agent.notes_indexer")

_CLIENT = None


def _parser_client():
    global _CLIENT
    if _CLIENT is None:
        from parser_client import ParserClient
        _CLIENT = ParserClient()
    return _CLIENT


def chunk_note(title: str, body: str, max_chars: int = 2000) -> list[dict]:
    text = f"# {title}\n{body}" if title else body
    chunks = []
    for i in range(0, max(len(text), 1), max_chars):
        chunks.append({"chunk_no": len(chunks), "text": text[i:i + max_chars]})
    return chunks


async def index_note(note: dict, body: str) -> bool:
    client = _CLIENT or _parser_client()
    try:
        await client.notes_upsert(
            user_id=note["user_id"], note_id=note["id"],
            note_type=note["type"], status=note["status"],
            created_by=note["created_by"], updated_at=note["updated_at"],
            chunks=chunk_note(note.get("title", ""), body))
        return True
    except Exception as e:
        _LOG.warning("index_note failed for %s: %s", note.get("id", "?"), e)
        return False


async def deindex_note(user_id: str, note_id: str) -> bool:
    client = _CLIENT or _parser_client()
    try:
        await client.notes_delete(user_id, note_id)
        return True
    except Exception as e:
        _LOG.warning("deindex_note failed for %s: %s", note_id, e)
        return False
