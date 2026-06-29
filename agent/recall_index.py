"""Conversation recall indexing (P2). After a session goes idle, a startup
worker indexes ONLY the messages added since sessions.recall_indexed_msgs,
as new chunks continuing sessions.recall_chunk_seq, and asks Parser to embed +
upsert them into the Qdrant `agent_memory` collection. Append-only incremental:
never re-chunks/re-embeds old content (no waste, no ghost chunks). Fully async —
never on the chat main path; embed runs on Parser's resident BGE-M3 (no LLM,
no creds, no per-user lock)."""
from __future__ import annotations

import asyncio
import logging
import time

import memory_store

_LOG = logging.getLogger("nimoos-agent.recall_index")

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


def _claim_idle_job(conn, now):
    return conn.execute(
        "SELECT * FROM recall_index_jobs "
        "WHERE status='pending' AND enqueued_at <= ? "
        "ORDER BY enqueued_at ASC LIMIT 1",
        (now - IDLE_SECONDS,),
    ).fetchone()


def _fail_job(conn, session_id, attempts, err, now):
    # conditional on status='running': a re-enqueue (→ pending) during the
    # attempt must not be clobbered.
    if attempts >= MAX_ATTEMPTS:
        conn.execute("DELETE FROM recall_index_jobs "
                     "WHERE session_id=? AND status='running'", (session_id,))
    else:
        conn.execute(
            "UPDATE recall_index_jobs SET status='pending', last_error=?, "
            "updated_at=? WHERE session_id=? AND status='running'",
            (err, now, session_id))
    conn.commit()


def _read_offset(conn, session_id):
    r = conn.execute("SELECT recall_indexed_msgs, recall_chunk_seq "
                     "FROM sessions WHERE id=?", (session_id,)).fetchone()
    if r is None:
        return 0, 0
    return r["recall_indexed_msgs"] or 0, r["recall_chunk_seq"] or 0


async def process_pending_once(conn, *, upsert_call, history_loader, now=None):
    now = now if now is not None else int(time.time())
    job = _claim_idle_job(conn, now)
    if job is None:
        return None
    session_id, user_id = job["session_id"], job["user_id"]
    attempts = job["attempts"] + 1
    conn.execute("UPDATE recall_index_jobs SET status='running', attempts=?, "
                 "updated_at=? WHERE session_id=?", (attempts, now, session_id))
    conn.commit()

    indexed_msgs, chunk_seq = _read_offset(conn, session_id)
    history = history_loader(session_id)
    new_msgs = history[indexed_msgs:]
    chunks = chunk_messages(new_msgs, start_chunk_no=chunk_seq, now=now)

    if not chunks:
        # nothing new to index — advance the msg cursor, drop the job
        conn.execute("UPDATE sessions SET recall_indexed_msgs=? WHERE id=?",
                     (len(history), session_id))
        conn.execute("DELETE FROM recall_index_jobs "
                     "WHERE session_id=? AND status='running'", (session_id,))
        conn.commit()
        return session_id

    try:
        for i in range(0, len(chunks), MAX_UPSERT_CHUNKS):
            batch = chunks[i:i + MAX_UPSERT_CHUNKS]
            await asyncio.wait_for(upsert_call(user_id, session_id, batch),
                                   timeout=UPSERT_TIMEOUT)
    except Exception as e:
        _LOG.warning("recall index failed for %s: %s", session_id, e)
        _fail_job(conn, session_id, attempts, str(e), now)   # offset NOT advanced
        return session_id

    conn.execute(
        "UPDATE sessions SET recall_indexed_msgs=?, recall_chunk_seq=? "
        "WHERE id=?", (len(history), chunk_seq + len(chunks), session_id))
    conn.execute("DELETE FROM recall_index_jobs "
                 "WHERE session_id=? AND status='running'", (session_id,))
    conn.commit()
    _LOG.info("recall indexed %s: +%d chunks", session_id, len(chunks))
    return session_id


_parser_client = None


def _get_parser_client():
    global _parser_client
    if _parser_client is None:
        from parser_client import ParserClient
        _parser_client = ParserClient()
    return _parser_client


async def _default_upsert(user_id, session_id, chunks):
    await _get_parser_client().agent_memory_upsert(user_id, session_id, chunks)


def _default_history_loader(session_id) -> list:
    import db
    import json as _json
    row = db.get_connection().execute(
        "SELECT content FROM messages WHERE session_id=? "
        "ORDER BY created_at DESC LIMIT 1", (session_id,)).fetchone()
    if not row:
        return []
    try:
        h = _json.loads(row["content"])
        return h if isinstance(h, list) else []
    except (ValueError, KeyError):
        return []


async def worker_loop(conn, *, stop_event):
    while not stop_event.is_set():
        try:
            await process_pending_once(conn, upsert_call=_default_upsert,
                                       history_loader=_default_history_loader)
        except Exception as e:
            _LOG.exception("recall worker tick error: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_SECONDS)
        except asyncio.TimeoutError:
            pass


def start_worker(conn):
    stop_event = asyncio.Event()
    task = asyncio.create_task(worker_loop(conn, stop_event=stop_event))
    return task, stop_event
