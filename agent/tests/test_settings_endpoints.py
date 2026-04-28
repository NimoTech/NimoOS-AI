import pytest
from httpx import AsyncClient, ASGITransport

import main as main_module


@pytest.mark.asyncio
async def test_get_thinking_defaults_returns_initial(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/agent/user-settings/thinking",
                         headers={"X-User-Id": "u1"})
    assert r.status_code == 200
    body = r.json()
    assert body == {"enabled": True, "level": "medium"}


@pytest.mark.asyncio
async def test_put_then_get_thinking_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.put(
            "/agent/user-settings/thinking",
            headers={"X-User-Id": "u1"},
            json={"enabled": False, "level": "high"},
        )
        r = await ac.get("/agent/user-settings/thinking",
                         headers={"X-User-Id": "u1"})
    assert r.json() == {"enabled": False, "level": "high"}


@pytest.mark.asyncio
async def test_patch_session_thinking(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post("/agent/sessions",
                      headers={"X-User-Id": "u1"},
                      json={"title": "t"})
        # find the session id via list endpoint (existing)
        r = await ac.get("/agent/sessions", headers={"X-User-Id": "u1"})
        sid = r.json()[0]["id"]

        r = await ac.patch(
            f"/agent/sessions/{sid}/thinking",
            headers={"X-User-Id": "u1"},
            json={"enabled": True, "level": "max"},
        )
        assert r.status_code == 200
        # Verify by reading the session row directly
        conn = main_module._db()
        row = conn.execute(
            "SELECT thinking_enabled, thinking_level FROM sessions WHERE id=?",
            (sid,),
        ).fetchone()
        assert row["thinking_enabled"] == 1
        assert row["thinking_level"] == "max"
