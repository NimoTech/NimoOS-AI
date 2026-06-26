"""Conversation recall indexing (P2). After a session goes idle, a startup
worker indexes ONLY the messages added since sessions.recall_indexed_msgs,
as new chunks continuing sessions.recall_chunk_seq, and asks Parser to embed +
upsert them into the Qdrant `agent_memory` collection. Append-only incremental:
never re-chunks/re-embeds old content (no waste, no ghost chunks). Fully async —
never on the chat main path; embed runs on Parser's resident BGE-M3 (no LLM,
no creds, no per-user lock)."""
from __future__ import annotations

import time

import memory_store

POLL_SECONDS = 30
IDLE_SECONDS = 120
MAX_ATTEMPTS = 3
CHUNK_MAX_CHARS = 2000
MAX_UPSERT_CHUNKS = 32
UPSERT_TIMEOUT = 30


def maybe_enqueue_index_job(conn, session_id, user_id, *, now=None) -> bool:
    """UPSERT a per-session recall-index job (coalescing) iff memory is enabled.
    Returns True when (re)enqueued."""
    if not memory_store.is_memory_enabled(conn, user_id):
        return False
    now = now if now is not None else int(time.time())
    conn.execute(
        "INSERT INTO recall_index_jobs "
        "(session_id, user_id, status, attempts, last_error, enqueued_at, updated_at) "
        "VALUES (?,?, 'pending', 0, NULL, ?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        " status='pending', attempts=0, last_error=NULL, "
        " enqueued_at=excluded.enqueued_at, updated_at=excluded.updated_at",
        (session_id, str(user_id), now, now),
    )
    conn.commit()
    return True


def chunk_messages(messages, *, start_chunk_no, now,
                   max_chars=CHUNK_MAX_CHARS) -> list[dict]:
    """Group the given (already-sliced, new) messages into chunks under
    max_chars. chunk_no starts at start_chunk_no and increments — so each run
    appends new chunks without renumbering earlier ones. Skips empty content."""
    chunks: list[dict] = []
    buf: list[str] = []
    size = 0
    n = start_chunk_no
    for m in messages:
        content = m.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        line = f"{m.get('role', '')}: {content}".strip()
        if not line or content.strip() == "":
            continue
        if buf and size + len(line) > max_chars:
            chunks.append({"chunk_no": n, "text": "\n".join(buf),
                           "created_at": now})
            n += 1
            buf, size = [], 0
        buf.append(line)
        size += len(line)
    if buf:
        chunks.append({"chunk_no": n, "text": "\n".join(buf), "created_at": now})
    return chunks
