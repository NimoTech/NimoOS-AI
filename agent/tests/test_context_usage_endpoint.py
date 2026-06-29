"""Tests for GET /agent/context-usage endpoint.

Reuses the same fixture pattern as test_main_user_memory.py:
  - monkeypatch main_module._DB_PATH to a tmp path
  - call main_module._db() to get the isolated connection
  - use ASGITransport + AsyncClient with X-User-Id header
"""
import json

import pytest
from httpx import AsyncClient, ASGITransport

import main as main_module
import context_compaction as cc


def _sess(conn, sid="s1", user="u1", summary=None):
    conn.execute(
        "INSERT INTO sessions(id,user_id,created_at,updated_at,rolling_summary) "
        "VALUES(?,?,0,0,?)",
        (sid, user, summary),
    )
    conn.commit()


def _snapshot(conn, sid, history):
    conn.execute(
        "INSERT INTO messages(id,session_id,role,content,created_at) "
        "VALUES(?,?,?,?,?)",
        (sid + "-m", sid, "history", json.dumps(history), 1),
    )
    conn.commit()


# --- tests -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_session_returns_zero(tmp_path, monkeypatch):
    """1) Empty session → 200 {tokens:0, window:128000, pct:0} for gpt-4o."""
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    conn = main_module._db()
    _sess(conn, sid="s1", user="u1")

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(
            "/agent/context-usage",
            params={"session_id": "s1", "model": "gpt-4o"},
            headers={"X-User-Id": "u1"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["tokens"] == 0
    assert body["window"] == 128000
    assert body["pct"] == 0


@pytest.mark.asyncio
async def test_seeded_session_returns_nonzero_tokens(tmp_path, monkeypatch):
    """2) Seeded messages → tokens>0 and pct==round(100*tokens/window)."""
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    conn = main_module._db()
    _sess(conn, sid="s1", user="u1")
    _snapshot(conn, "s1", [
        {"role": "user", "content": "你好" * 50},
        {"role": "assistant", "content": "回答" * 50},
    ])

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(
            "/agent/context-usage",
            params={"session_id": "s1", "model": "gpt-4o"},
            headers={"X-User-Id": "u1"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["tokens"] > 0
    assert body["pct"] == round(100 * body["tokens"] / body["window"])


@pytest.mark.asyncio
async def test_missing_session_id_returns_400(tmp_path, monkeypatch):
    """3) Missing session_id → 400."""
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    main_module._db()

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(
            "/agent/context-usage",
            headers={"X-User-Id": "u1"},
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_missing_user_id_returns_401(tmp_path, monkeypatch):
    """4) Missing X-User-Id header → 401."""
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    main_module._db()

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(
            "/agent/context-usage",
            params={"session_id": "s1"},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_model_omitted_uses_default_context_window(tmp_path, monkeypatch):
    """5) model omitted → window == DEFAULT_CONTEXT_WINDOW (8192)."""
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    conn = main_module._db()
    _sess(conn, sid="s1", user="u1")

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(
            "/agent/context-usage",
            params={"session_id": "s1"},
            headers={"X-User-Id": "u1"},
        )
    assert r.status_code == 200
    assert r.json()["window"] == cc.DEFAULT_CONTEXT_WINDOW
