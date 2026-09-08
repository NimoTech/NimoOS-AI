"""Per-model context windows (spec §6.1, ruling P3-R1: agent.db is the single
store). Rows: model_key -> (window, source) with source manual | fetched |
learned. Manual is never overwritten by machines; learned only shrinks."""
from __future__ import annotations

import logging
import time

import context_compaction as cc

_LOG = logging.getLogger("nimoos-agent.model_windows")

SOURCES = ("manual", "fetched", "learned")


def _bare_name(model_name: str) -> str:
    name = (model_name or "").strip()
    low = name.lower()
    if low.startswith("local:"):
        return name[6:]
    if low.startswith("cloud:"):
        rest = name[6:]
        return rest.split(":", 1)[1] if ":" in rest else rest
    return name


def model_key(model_name: str, provider_type: str = "") -> str:
    raw = (model_name or "").strip()
    local = provider_type == "ollama" or raw.lower().startswith("local:")
    return ("local:" if local else "cloud:") + _bare_name(raw).lower()


def get(conn, key: str) -> dict | None:
    row = conn.execute("SELECT model_key, window, source, updated_at FROM model_windows WHERE model_key=?",
                       (key,)).fetchone()
    return dict(row) if row else None


def upsert(conn, key: str, window: int, source: str) -> None:
    if source not in SOURCES:
        raise ValueError(f"bad source {source!r}")
    window = int(window)
    if window < cc.MIN_CONTEXT_WINDOW:
        raise ValueError(f"window {window} < MIN_CONTEXT_WINDOW {cc.MIN_CONTEXT_WINDOW}")
    cur = get(conn, key)
    if cur is not None:
        if cur["source"] == "manual" and source != "manual":
            return                                   # humans win
        if source == "learned" and cur["source"] == "learned" and window >= cur["window"]:
            return                                   # learned only shrinks
        if source == "learned" and cur["source"] == "fetched" and window >= cur["window"]:
            return
    conn.execute("INSERT INTO model_windows(model_key, window, source, updated_at) VALUES(?,?,?,?) "
                 "ON CONFLICT(model_key) DO UPDATE SET window=excluded.window, source=excluded.source, "
                 "updated_at=excluded.updated_at", (key, window, source, int(time.time())))
    conn.commit()


def delete_manual(conn, key: str) -> None:
    conn.execute("DELETE FROM model_windows WHERE model_key=? AND source='manual'", (key,))
    conn.commit()


def resolve_stored(conn, key: str) -> tuple[int, str] | None:
    row = get(conn, key)
    return (int(row["window"]), row["source"]) if row else None


import re

import httpx

FETCH_TIMEOUT = 3.0
_TRIED: set[str] = set()          # model keys already probed in this process
_NUM_CTX_RE = re.compile(r"^\s*num_ctx\s+(\d+)", re.M)
_LIST_FIELDS = ("context_length", "context_window", "max_context_length", "max_input_tokens")


def _to_int(v) -> int | None:
    try:
        n = int(str(v).strip())
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _parse_ollama_show(payload: dict) -> int | None:
    params = payload.get("parameters") if isinstance(payload, dict) else None
    if isinstance(params, str):
        m = _NUM_CTX_RE.search(params)
        if m:
            return _to_int(m.group(1))
    info = payload.get("model_info") if isinstance(payload, dict) else None
    if isinstance(info, dict):
        for k, v in info.items():
            if str(k).endswith(".context_length"):
                n = _to_int(v)
                if n:
                    return n
    return None


def _parse_models_list(payload: dict, model_name: str) -> int | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    for entry in data:
        if isinstance(entry, dict) and str(entry.get("id", "")) == model_name:
            for f in _LIST_FIELDS:
                n = _to_int(entry.get(f))
                if n:
                    return n
            return None
    return None


def _ollama_base(url: str) -> str:
    base = (url or "").strip().rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


async def fetch_window(provider_type: str, provider_url: str, model_name: str, api_key: str = "",
                       *, timeout: float = FETCH_TIMEOUT) -> int | None:
    """Ask the provider for the model's context length. Best effort: None on
    any failure, never raises, bounded by `timeout` seconds in total."""
    name = _bare_name(model_name)
    if not provider_url or not name:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if provider_type == "ollama":
                r = await client.post(_ollama_base(provider_url) + "/api/show", json={"name": name})
                return _parse_ollama_show(r.json()) if r.status_code == 200 else None
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
            r = await client.get(provider_url.strip().rstrip("/") + "/models", headers=headers)
            return _parse_models_list(r.json(), name) if r.status_code == 200 else None
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("fetch_window(%s, %s) failed: %s", provider_type, name, exc)
        return None


async def ensure_fetched(conn, *, provider_type: str, provider_url: str, model_name: str,
                         api_key: str = "") -> None:
    """Populate a `fetched` row once per process unless a manual/fetched row
    already exists. Never raises; never blocks longer than FETCH_TIMEOUT."""
    key = model_key(model_name, provider_type)
    if key in _TRIED:
        return
    cur = get(conn, key)
    if cur is not None and cur["source"] in ("manual", "fetched"):
        return
    _TRIED.add(key)
    w = await fetch_window(provider_type, provider_url, model_name, api_key)
    if w and w >= cc.MIN_CONTEXT_WINDOW:
        try:
            upsert(conn, key, w, "fetched")
            _LOG.info("model_windows: fetched %s = %d", key, w)
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("model_windows: store failed: %s", exc)


def learn(conn, key: str, window: int) -> int:
    """Record a window learned from a context-limit 400 (only ever shrinks a
    machine-written row; never touches a manual one). Returns the row's
    effective window afterwards."""
    cur = get(conn, key)
    if cur is not None and cur["source"] == "manual":
        return int(cur["window"])
    target = int(window)
    if cur is not None:
        target = min(target, int(cur["window"]))
    try:
        upsert(conn, key, target, "learned")
    except ValueError:
        return int(cur["window"]) if cur else target
    return target
