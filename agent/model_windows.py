"""Per-model context windows (spec §6.1, ruling P3-R1: agent.db is the single
store). Rows: model_key -> (window, source) with source manual | fetched |
learned. Manual is never overwritten by machines; learned only shrinks."""
from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit

import context_compaction as cc

_LOG = logging.getLogger("nimoos-agent.model_windows")

SOURCES = ("manual", "fetched", "learned")
_RANK = {"manual": 3, "fetched": 2, "learned": 1}


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
