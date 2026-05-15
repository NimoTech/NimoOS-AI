import os
import shutil
import sqlite3
import time


def run_startup_gc(conn: sqlite3.Connection, data_root: str,
                   age_seconds: int = 86400, now: int | None = None) -> None:
    """
    Two passes:
      1. Delete draft attachments (message_id IS NULL) older than age_seconds.
      2. Recursively remove sessions/<sid>/ dirs that have no matching sessions row.
    Both passes are best-effort (ignore_errors=True for filesystem ops).
    """
    if now is None:
        now = int(time.time())
    cutoff = now - age_seconds

    # Pass 1: old drafts
    rows = conn.execute(
        "SELECT id, session_id, rel_path FROM attachments "
        "WHERE message_id IS NULL AND created_at < ?",
        (cutoff,),
    ).fetchall()
    for r in rows:
        aid = r["id"] if isinstance(r, sqlite3.Row) else r[0]
        sid = r["session_id"] if isinstance(r, sqlite3.Row) else r[1]
        rel = r["rel_path"] if isinstance(r, sqlite3.Row) else r[2]
        full = os.path.join(data_root, "sessions", sid, "attachments", rel)
        try:
            os.remove(full)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        conn.execute("DELETE FROM attachments WHERE id = ?", (aid,))
    conn.commit()

    # Pass 2: orphan session dirs
    sessions_root = os.path.join(data_root, "sessions")
    if not os.path.isdir(sessions_root):
        return
    known = {r[0] for r in conn.execute("SELECT id FROM sessions")}
    for entry in os.listdir(sessions_root):
        if entry in known:
            continue
        shutil.rmtree(os.path.join(sessions_root, entry), ignore_errors=True)
