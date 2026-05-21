"""Service URL discovery via /var/run/nimoos/*.url files.

Wiki and AI services write http://127.0.0.1:<random> to these files on
startup. This module reads them and returns the URLs.
"""
from __future__ import annotations
import sqlite3
from pathlib import Path


class DiscoveryError(Exception):
    """Raised when a required service URL file is missing or unreadable.
    Worker treats this as a transient failure — break the round, retry next
    timer fire."""


_RUNTIME_DIR = Path("/var/run/nimoos")


def wiki_url() -> str:
    return _read(_RUNTIME_DIR / "wiki.url")


def ai_url() -> str:
    return _read(_RUNTIME_DIR / "ai.url")


def _read(p: Path) -> str:
    try:
        content = p.read_text().strip()
    except OSError as e:
        raise DiscoveryError(f"cannot read {p}: {e}") from e
    if not content.startswith("http://"):
        raise DiscoveryError(f"{p} contains unexpected content: {content!r}")
    return content


_USERS_DB = Path("/var/lib/nimoos/db/user.db")


def resolve_user_id(cfg) -> str:
    """Pick the X-NimoOS-User-ID header value for chat-completions calls.

    Order of preference:
      1. cfg.user_id_header if non-empty (operator override)
      2. lowest-ID user with role='admin' in /var/lib/nimoos/db/user.db
      3. lowest-ID user in that table regardless of role
      4. literal "system" as last-resort fallback

    The fallback to "system" exists so the worker doesn't crash on a
    machine without user.db; on such a setup chat-completions will route
    to local Ollama (which is the only sensible thing anyway).
    """
    if cfg.user_id_header:
        return cfg.user_id_header

    try:
        conn = sqlite3.connect(f"file:{_USERS_DB}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return "system"
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM o_users WHERE role='admin' ORDER BY id LIMIT 1")
        row = cur.fetchone()
        if row is not None:
            return str(row[0])
        cur.execute("SELECT id FROM o_users ORDER BY id LIMIT 1")
        row = cur.fetchone()
        if row is not None:
            return str(row[0])
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return "system"
