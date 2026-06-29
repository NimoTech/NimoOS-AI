"""Tests for compaction_enabled + context_window fields in the memory settings API.

Mirrors the fixture/auth style of test_main_user_memory.py (AsyncClient +
X-User-Id header, monkeypatched _DB_PATH).
"""
import pytest
from httpx import AsyncClient, ASGITransport
import main as main_module


@pytest.mark.asyncio
async def test_get_defaults_compaction_enabled_true_context_window_none(
    tmp_path, monkeypatch
):
    """GET with no prior state returns compaction_enabled True, context_window None."""
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    main_module._db()
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(
            "/agent/user-memory/settings", headers={"X-User-Id": "u1"}
        )
    assert r.status_code == 200
    body = r.json()
    assert body["compaction_enabled"] is True
    assert body["context_window"] is None


@pytest.mark.asyncio
async def test_put_compaction_enabled_false_then_get_returns_false(
    tmp_path, monkeypatch
):
    """PUT compaction_enabled False → GET returns compaction_enabled False."""
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    main_module._db()
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        p = await ac.put(
            "/agent/user-memory/settings",
            headers={"X-User-Id": "u1"},
            json={"enabled": True, "compaction_enabled": False},
        )
        assert p.status_code == 200
        assert p.json()["compaction_enabled"] is False
        g = await ac.get(
            "/agent/user-memory/settings", headers={"X-User-Id": "u1"}
        )
    assert g.status_code == 200
    assert g.json()["compaction_enabled"] is False


@pytest.mark.asyncio
async def test_put_context_window_50000_then_get_returns_50000(
    tmp_path, monkeypatch
):
    """PUT context_window 50000 → GET returns 50000."""
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    main_module._db()
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        p = await ac.put(
            "/agent/user-memory/settings",
            headers={"X-User-Id": "u1"},
            json={"enabled": True, "context_window": 50000},
        )
        assert p.status_code == 200
        assert p.json()["context_window"] == 50000
        g = await ac.get(
            "/agent/user-memory/settings", headers={"X-User-Id": "u1"}
        )
    assert g.status_code == 200
    assert g.json()["context_window"] == 50000


@pytest.mark.asyncio
async def test_put_context_window_zero_then_get_returns_none(tmp_path, monkeypatch):
    """PUT context_window 0 (invalid) → GET returns context_window None."""
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    main_module._db()
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # First set a valid value to confirm it gets cleared
        await ac.put(
            "/agent/user-memory/settings",
            headers={"X-User-Id": "u1"},
            json={"enabled": True, "context_window": 50000},
        )
        p = await ac.put(
            "/agent/user-memory/settings",
            headers={"X-User-Id": "u1"},
            json={"enabled": True, "context_window": 0},
        )
        assert p.status_code == 200
        assert p.json()["context_window"] is None
        g = await ac.get(
            "/agent/user-memory/settings", headers={"X-User-Id": "u1"}
        )
    assert g.status_code == 200
    assert g.json()["context_window"] is None


@pytest.mark.asyncio
async def test_existing_enabled_field_not_regressed(tmp_path, monkeypatch):
    """The existing 'enabled' field still works correctly after adding new fields."""
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    main_module._db()
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Default: enabled True
        g = await ac.get(
            "/agent/user-memory/settings", headers={"X-User-Id": "u1"}
        )
        assert g.json()["enabled"] is True

        # PUT enabled False
        p = await ac.put(
            "/agent/user-memory/settings",
            headers={"X-User-Id": "u1"},
            json={"enabled": False},
        )
        assert p.status_code == 200
        assert p.json()["enabled"] is False

        # GET returns False
        g2 = await ac.get(
            "/agent/user-memory/settings", headers={"X-User-Id": "u1"}
        )
        assert g2.json()["enabled"] is False

    # Verify DB row
    conn = main_module._db()
    row = conn.execute(
        "SELECT value FROM user_settings WHERE user_id='u1' AND key='memory_enabled'"
    ).fetchone()
    assert row["value"] == "0"


@pytest.mark.asyncio
async def test_settings_requires_user_id_still_401(tmp_path, monkeypatch):
    """GET/PUT without X-User-Id still returns 401."""
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        g = await ac.get("/agent/user-memory/settings")
        p = await ac.put(
            "/agent/user-memory/settings", json={"enabled": True}
        )
    assert g.status_code == 401
    assert p.status_code == 401
