"""Session-idle memory auto-extraction (P3). A startup asyncio worker, after a
session goes quiet, asks the conversation's own model to distill durable user
facts and applies ADD/UPDATE/NOOP to the profile store. Fully async — never on
the chat main path; bounded by hard timeout + attempt cap + sequential single-flight.
"""
from __future__ import annotations

import time

import memory_store

POLL_SECONDS = 30
IDLE_SECONDS = 120
MAX_ATTEMPTS = 3
LLM_TIMEOUT = 60
HISTORY_MAX_CHARS = 12000


def maybe_enqueue_extract_job(conn, session_id, user_id, *, provider_url,
                              provider_key, provider_type, model_name,
                              now=None) -> bool:
    """UPSERT a per-session extraction job (coalescing) iff memory is enabled
    for the user. Refreshes the credential snapshot + enqueued_at. Returns
    True when a job is (re)enqueued."""
    if not memory_store.is_memory_enabled(conn, user_id):
        return False
    now = now if now is not None else int(time.time())
    conn.execute(
        "INSERT INTO memory_extract_jobs "
        "(session_id, user_id, status, attempts, provider_url, provider_key, "
        " provider_type, model_name, last_error, enqueued_at, updated_at) "
        "VALUES (?,?, 'pending', 0, ?,?,?,?, NULL, ?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        " status='pending', attempts=0, provider_url=excluded.provider_url, "
        " provider_key=excluded.provider_key, provider_type=excluded.provider_type, "
        " model_name=excluded.model_name, last_error=NULL, "
        " enqueued_at=excluded.enqueued_at, updated_at=excluded.updated_at",
        (session_id, str(user_id), provider_url, provider_key, provider_type,
         model_name, now, now),
    )
    conn.commit()
    return True
