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


def resolve_model_and_routing(cfg) -> tuple:
    """Pick (model_name, force_cloud) for the next chat-completions call.

    Order of preference:
      1. cfg.model non-empty → use it as-is, force_cloud=False (let user's
         privacy policy on ai.db decide local/cloud routing).
      2. cfg.model empty → query /v1/ai/_internal/models?user_id=X:
         a. if local list non-empty → return (local[0]["name"], False)
         b. else if cloud list non-empty → return (cloud[0]["default_model"], True)
         c. both empty → raise RuntimeError (no model available; the worker
            will treat this as transient and break the round).

    The force_cloud=True case sets X-NimoOS-Force-Cloud header so ai-service's
    Router.Decide bypasses the user's local-by-default policy when there's no
    local model installed.
    """
    if cfg.model:
        return cfg.model, False

    import httpx  # local import — discovery.resolve_user_id doesn't need it
    user_id = resolve_user_id(cfg)
    try:
        with httpx.Client(timeout=5) as c:
            r = c.get(ai_url() + "/v1/ai/_internal/models",
                      params={"user_id": user_id})
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        raise RuntimeError(f"cannot resolve model: /_internal/models failed: {e}") from e

    local = data.get("local") or []
    cloud = data.get("cloud") or []

    if local:
        return local[0]["name"], False
    if cloud:
        return cloud[0]["default_model"], True

    raise RuntimeError("no model available: local Ollama empty and no enabled cloud providers")


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
