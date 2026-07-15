"""Persistent shell command allowlist. Entries never expire (no TTL);
managed via HTTP endpoints. Matched commands auto-run even unattended."""
from __future__ import annotations

import os
import re
import time
import uuid


def add(conn, match_type: str, value: str, created_by: str, note: str = "") -> str:
    if match_type not in ("prefix", "regex", "path_scope"):
        raise ValueError(f"bad match_type: {match_type}")
    entry_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO shell_allowlist "
        "(id, match_type, value, created_by, note, created_at) VALUES (?,?,?,?,?,?)",
        (entry_id, match_type, value, created_by, note, int(time.time())),
    )
    conn.commit()
    return entry_id


def list_entries(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, match_type, value, created_by, note, created_at "
        "FROM shell_allowlist ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def delete(conn, entry_id: str) -> bool:
    cur = conn.execute("DELETE FROM shell_allowlist WHERE id=?", (entry_id,))
    conn.commit()
    return cur.rowcount > 0


def _entry_matches(command: str, match_type: str, value: str) -> bool:
    if match_type == "prefix":
        return command.strip().startswith(value)
    if match_type == "regex":
        try:
            return re.search(value, command) is not None
        except re.error:
            return False  # invalid stored regex never matches
    if match_type == "path_scope":
        scope = os.path.realpath(value)
        for tok in command.split():
            if tok.startswith("/") or "/" in tok:
                rp = os.path.realpath(tok)
                if rp == scope or rp.startswith(scope + "/"):
                    return True
        return False
    return False


def match(conn, command: str) -> bool:
    rows = conn.execute(
        "SELECT match_type, value FROM shell_allowlist").fetchall()
    return any(_entry_matches(command, r["match_type"], r["value"]) for r in rows)
