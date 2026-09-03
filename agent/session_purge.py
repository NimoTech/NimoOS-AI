"""Delete a session and everything that hangs off it — one implementation.

There used to be two copies of this: `main.delete_session` (the HTTP handler)
and `tasks/runner.delete_session` (used when run history is pruned). They
drifted immediately — the M2 review found the runner's copy leaving
`agent_runs`/`event_log` behind, which is where the bulk of the bytes are
(a measured ~591 `event_log` rows per run on the live box). Both callers now
go through `purge_session`, so the next table that needs clearing is added
once.

What must be deleted, and why the order is not negotiable:

* `messages.session_id` is `REFERENCES sessions(id)` **without** ON DELETE
  CASCADE and `PRAGMA foreign_keys=ON` is set at init, so deleting the session
  row first raises `IntegrityError: FOREIGN KEY constraint failed` for any
  session that ever exchanged a message. Children first, parent last.
* `event_log` is keyed by `run_id`, not `session_id`, so it has to be reached
  through `agent_runs` — and therefore before `agent_runs` is deleted, or the
  join that finds them is gone and the rows are unreachable forever. This is
  the wiki `file_events` failure class: rows only ever inserted, with no path
  that deletes.
* `agent_runs` and `event_log` have no FK at all — nothing reclaims them
  implicitly.
* The recall vectors would keep a deleted session's content answerable by the
  `recall` tool, and the snapshot directory would leak disk for the lifetime
  of the box.
* tool_output offload files (ROOT/<session_id>) would leak disk like snapshots.
* The three `*_extract_jobs`/`recall_index_jobs` rows are keyed by session_id
  and would be picked up by their workers after the session they describe is
  gone. One row each, but they are strictly useless once the messages are.

Tables with `ON DELETE CASCADE` (visible_resources, staged_changes,
attachments, access_requests) are handled by SQLite itself and are absent here
on purpose.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil

logger = logging.getLogger("nimoos-agent.session")

DEFAULT_SNAPSHOTS_ROOT = "/var/lib/nimoos/ai/agent/snapshots"

# Session-scoped worker queues. A job whose session no longer exists can only
# fail (or index nothing), so it goes with it.
_JOB_TABLES = ("memory_extract_jobs", "recall_index_jobs", "notes_extract_jobs")


async def default_vector_cleanup(user_id: str, session_id: str) -> None:
    import recall_index  # noqa: PLC0415
    await asyncio.wait_for(
        recall_index._get_parser_client().agent_memory_delete(user_id, session_id),
        timeout=10)


def default_snapshots_root() -> str:
    """Where fs-skill snapshots live. Resolved through `main` when it is
    importable so a test (or a non-default deployment) that moves the root is
    honored here too."""
    try:
        import main  # noqa: PLC0415
        return main._snapshots_root
    except Exception:                       # noqa: BLE001 — main not importable
        return os.environ.get("AGENT_SNAPSHOTS_ROOT", DEFAULT_SNAPSHOTS_ROOT)


async def purge_session(conn, user_id: str, session_id: str, *,
                        vector_cleanup=None, snapshots_root=None) -> bool:
    """Delete `session_id` and every row/file that belongs to it.

    Returns False without touching anything when the session exists but
    belongs to another user. (A missing session is not an error: the caller
    may be cleaning up after a partial delete, and every statement below is
    idempotent.)

    `vector_cleanup` is the test seam — `async (user_id, session_id) -> None`,
    defaulting to `default_vector_cleanup`. It is best-effort: deletion must
    never depend on Parser being up, a missed cleanup only leaves orphan
    vectors (logged).
    """
    owner = conn.execute("SELECT user_id FROM sessions WHERE id=?",
                         (session_id,)).fetchone()
    if owner is not None and str(owner["user_id"]) != str(user_id):
        logger.warning("refusing to delete session %s owned by another user",
                       session_id)
        return False

    cleanup = vector_cleanup or default_vector_cleanup
    try:
        await cleanup(user_id, session_id)
    except Exception as exc:                # noqa: BLE001 — soft-fail
        logger.warning("vector cleanup failed for session %s: %s",
                       session_id, exc)

    # event_log before agent_runs: the subquery is the only way back to them.
    conn.execute(
        "DELETE FROM event_log WHERE run_id IN "
        "(SELECT id FROM agent_runs WHERE session_id=?)", (session_id,))
    conn.execute("DELETE FROM agent_runs WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM pending_confirmations WHERE session_id=?",
                 (session_id,))
    for table in _JOB_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    conn.commit()

    root = snapshots_root or default_snapshots_root()
    shutil.rmtree(os.path.join(root, session_id), ignore_errors=True)

    # Offloaded tool results (tool_output.py) live outside the DB too.
    try:
        import tool_output as _to  # noqa: PLC0415
        shutil.rmtree(_to.chat_dir_for_session(session_id), ignore_errors=True)
    except Exception:  # noqa: BLE001
        logger.debug("tool_output cleanup skipped for %s", session_id, exc_info=True)
    return True
