# NimoOS-AI/agent/mcp_tokens.py
"""Long-lived API tokens for the MCP server. Stores only sha256(token).
Each token maps to a NimoOS user_id; all MCP tools run in that user's scope."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid

_PREFIX = "nimoos_mcp_"
_TOUCH_INTERVAL_MS = 60_000  # throttle last_used_at writes


def generate_token() -> str:
    return _PREFIX + secrets.token_hex(24)  # 48 hex chars


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create(conn: sqlite3.Connection, user_id: str, label: str,
           now_ms: int) -> tuple[str, str]:
    token = generate_token()
    tok_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO mcp_tokens (id, user_id, label, token_hash, created_at) "
        "VALUES (?,?,?,?,?)",
        (tok_id, str(user_id), label or "", _hash(token), now_ms),
    )
    conn.commit()
    return tok_id, token


def verify(conn: sqlite3.Connection, token: str, now_ms: int) -> str | None:
    if not token or not token.startswith(_PREFIX):
        return None
    row = conn.execute(
        "SELECT id, user_id, last_used_at FROM mcp_tokens "
        "WHERE token_hash=? AND revoked=0",
        (_hash(token),),
    ).fetchone()
    if row is None:
        return None
    last = row["last_used_at"] or 0
    if now_ms - last > _TOUCH_INTERVAL_MS:
        try:
            conn.execute("PRAGMA busy_timeout=2000")
            conn.execute("UPDATE mcp_tokens SET last_used_at=? WHERE id=?",
                         (now_ms, row["id"]))
            conn.commit()
        except sqlite3.OperationalError:
            pass  # best-effort; do not fail auth on a locked db
    return str(row["user_id"])


def list_for_user(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, label, created_at, last_used_at FROM mcp_tokens "
        "WHERE user_id=? AND revoked=0 ORDER BY created_at DESC",
        (str(user_id),),
    ).fetchall()
    return [dict(r) for r in rows]


def revoke(conn: sqlite3.Connection, user_id: str, token_id: str) -> bool:
    cur = conn.execute(
        "UPDATE mcp_tokens SET revoked=1 WHERE id=? AND user_id=? AND revoked=0",
        (token_id, str(user_id)),
    )
    conn.commit()
    return cur.rowcount > 0
