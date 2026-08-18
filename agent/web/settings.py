"""Web-tools configuration — one global row in user_settings.

The whole box shares one search backend: the NAS owner pays for the key, so
this is admin-scoped configuration rather than a per-user preference. Storage
follows phoenix_tracing.py's precedent — user_settings with the reserved
`__global__` user_id.

BACKEND_HOSTS is deliberately a fixed table, NOT a user-supplied field: this
set feeds the egress-confirm auto-approve path in main.py, so a free-text
"also trust this domain" input here would be a hole straight through the
proxy's TOFU gate.
"""
from __future__ import annotations

import json
import time
from urllib.parse import urlparse

_GLOBAL_SCOPE = "__global__"   # reserved user_id; protect in user-cleanup logic
_KEY = "web_search"

VALID_BACKENDS: tuple[str, ...] = ("tavily", "brave", "searxng")

BACKEND_HOSTS: dict[str, str] = {
    "tavily": "api.tavily.com",
    "brave": "api.search.brave.com",
}

_DEFAULTS = {"backend": "", "api_key": "", "base_url": "", "enabled": False}


def load(conn) -> dict:
    """Return the stored config, or defaults. Never raises."""
    try:
        row = conn.execute(
            "SELECT value FROM user_settings WHERE user_id=? AND key=?",
            (_GLOBAL_SCOPE, _KEY),
        ).fetchone()
    except Exception:  # noqa: BLE001 — a broken read must degrade, not crash a run
        return dict(_DEFAULTS)
    if not row:
        return dict(_DEFAULTS)
    try:
        doc = json.loads(row["value"])
    except (ValueError, TypeError):
        return dict(_DEFAULTS)
    if not isinstance(doc, dict):
        return dict(_DEFAULTS)
    return {
        "backend": str(doc.get("backend") or ""),
        "api_key": str(doc.get("api_key") or ""),
        "base_url": str(doc.get("base_url") or ""),
        "enabled": bool(doc.get("enabled")),
    }


def save(conn, *, backend: str, api_key: str, base_url: str,
         enabled: bool) -> None:
    doc = {"backend": backend, "api_key": api_key,
           "base_url": base_url, "enabled": bool(enabled)}
    conn.execute(
        "INSERT INTO user_settings(user_id, key, value, updated_at) "
        "VALUES(?, ?, ?, ?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, "
        "updated_at=excluded.updated_at",
        (_GLOBAL_SCOPE, _KEY, json.dumps(doc, ensure_ascii=False),
         int(time.time())),
    )
    conn.commit()


def public_view(cfg: dict) -> dict:
    """Config as the UI may see it — the key becomes a boolean and never leaves."""
    return {"backend": cfg["backend"], "base_url": cfg["base_url"],
            "enabled": cfg["enabled"], "has_key": bool(cfg["api_key"])}


def _host_of(base_url: str) -> str:
    try:
        return (urlparse(base_url).hostname or "").lower()
    except ValueError:
        return ""


def is_configured(cfg: dict) -> bool:
    """True when a backend is enabled AND carries what it needs to run."""
    if not cfg["enabled"] or cfg["backend"] not in VALID_BACKENDS:
        return False
    if cfg["backend"] == "searxng":
        return bool(_host_of(cfg["base_url"]))
    return bool(cfg["api_key"])


def preapproved_hosts(cfg: dict) -> set[str]:
    """Hosts that must not raise a TOFU card.

    Exact-host matching only — no suffix or wildcard match, and empty unless a
    backend is actually live. web_fetch's arbitrary targets never appear here:
    they must go through the confirmation card, which IS the domain gate.
    """
    if not is_configured(cfg):
        return set()
    if cfg["backend"] == "searxng":
        host = _host_of(cfg["base_url"])
        return {host} if host else set()
    host = BACKEND_HOSTS.get(cfg["backend"], "")
    return {host} if host else set()
