"""Document auto-precipitation worker (spec 2026-07-26): distills documents
under opted-in knowledge roots into draft `summary` notes. Fourth instance of
the memory_extract / recall_index / notes_extract coalescing-job skeleton —
enqueue happens only from the scanner (or an explicit manual request); the
worker NEVER polls the LLM on a timer (wiki summary worker lesson)."""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3

# Document subset of Parser's TEXT_EXT_ALLOWLIST. Source-code extensions are
# deliberately excluded: Parser does not error on unknown extensions (it reads
# them as replacement-char text), so a code repo under a knowledge root would
# otherwise flood the notes library with garbage summaries.
DISTILL_EXTS = frozenset({
    ".md", ".txt", ".rst",
    ".pdf",
    ".docx", ".doc", ".wps",
    ".pptx", ".ppt",
    ".xlsx", ".xls",
    ".odt",
    ".html", ".htm",
})


def is_distillable(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in DISTILL_EXTS


def enqueue(conn, *, file_path: str, user_id: str, root_id: str,
            file_mtime: int, origin: str = "auto", now=None) -> bool:
    """Coalescing UPSERT keyed on file_path. A manual request outranks a
    later auto one — never demote origin back to 'auto'."""
    if not is_distillable(file_path):
        return False
    now = int(time.time()) if now is None else now
    conn.execute(
        """INSERT INTO notes_distill_jobs
             (file_path, user_id, root_id, file_mtime, status, attempts,
              origin, enqueued_at, updated_at)
           VALUES (?,?,?,?,'pending',0,?,?,?)
           ON CONFLICT(file_path) DO UPDATE SET
             status='pending', attempts=0, last_error=NULL,
             user_id=excluded.user_id, root_id=excluded.root_id,
             file_mtime=excluded.file_mtime,
             origin=CASE WHEN notes_distill_jobs.origin='manual'
                         THEN 'manual' ELSE excluded.origin END,
             enqueued_at=excluded.enqueued_at,
             updated_at=excluded.updated_at""",
        (file_path, str(user_id), str(root_id), int(file_mtime), origin,
         now, now))
    conn.commit()
    return True


def claim_job(conn, *, quota_ok: bool, now=None):
    """Claim one pending job, manual first. Auto jobs are invisible once the
    daily quota is spent; manual jobs always jump the queue."""
    now = int(time.time()) if now is None else now
    sql = ("SELECT * FROM notes_distill_jobs WHERE status='pending' "
           "{extra} ORDER BY CASE origin WHEN 'manual' THEN 0 ELSE 1 END, "
           "enqueued_at ASC LIMIT 1")
    row = conn.execute(sql.format(
        extra="" if quota_ok else "AND origin='manual'")).fetchone()
    if row is None:
        return None
    attempts = row["attempts"] + 1
    conn.execute(
        "UPDATE notes_distill_jobs SET status='running', attempts=?, "
        "updated_at=? WHERE file_path=?", (attempts, now, row["file_path"]))
    conn.commit()
    return conn.execute("SELECT * FROM notes_distill_jobs WHERE file_path=?",
                        (row["file_path"],)).fetchone()


def finish_job(conn, file_path: str) -> None:
    conn.execute("DELETE FROM notes_distill_jobs "
                 "WHERE file_path=? AND status='running'", (file_path,))
    conn.commit()


def fail_job(conn, file_path: str, attempts: int, err, now: int) -> None:
    if attempts >= MAX_ATTEMPTS:
        conn.execute("DELETE FROM notes_distill_jobs "
                     "WHERE file_path=? AND status='running'", (file_path,))
    else:
        conn.execute(
            "UPDATE notes_distill_jobs SET status='pending', last_error=?, "
            "updated_at=? WHERE file_path=? AND status='running'",
            (str(err)[:500], now, file_path))
    conn.commit()


def requeue_orphaned(conn) -> int:
    """A row still 'running' at startup was claimed by a dead process — this
    worker is the table's only consumer, so flip it back to pending."""
    cur = conn.execute(
        "UPDATE notes_distill_jobs SET status='pending' WHERE status='running'")
    conn.commit()
    return cur.rowcount
