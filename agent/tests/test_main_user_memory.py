import pytest
from httpx import AsyncClient, ASGITransport
import main as main_module
import memory_store as ms


@pytest.mark.asyncio
async def test_list_returns_only_own_active_projected(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    conn = main_module._db()
    ms.add_memory(conn, "u1", "likes oat milk", "preference", source="tool", priority=5)
    dead = ms.add_memory(conn, "u1", "stale", "fact", source="auto")
    ms.disable_memory(conn, dead)
    ms.add_memory(conn, "u2", "other user", "fact", source="auto")
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/agent/user-memory", headers={"X-User-Id": "u1"})
    assert r.status_code == 200
    body = r.json()
    assert [m["text"] for m in body] == ["likes oat milk"]
    assert set(body[0].keys()) == {
        "id", "kind", "text", "source", "priority", "recall_count", "updated_at"}


@pytest.mark.asyncio
async def test_list_requires_user_id(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/agent/user-memory")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_delete_own_soft_disables(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    conn = main_module._db()
    mid = ms.add_memory(conn, "u1", "delete me", "fact", source="tool")
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.delete(f"/agent/user-memory/{mid}", headers={"X-User-Id": "u1"})
    assert r.status_code == 200
    assert r.json() == {"status": "deleted", "id": mid}
    assert ms.list_active(conn, "u1") == []


@pytest.mark.asyncio
async def test_delete_cross_user_is_404_and_keeps_active(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    conn = main_module._db()
    mid = ms.add_memory(conn, "u1", "u1 secret", "fact", source="tool")
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.delete(f"/agent/user-memory/{mid}", headers={"X-User-Id": "u2"})
    assert r.status_code == 404
    assert len(ms.list_active(conn, "u1")) == 1  # untouched


@pytest.mark.asyncio
async def test_delete_missing_is_404(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    main_module._db()
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.delete("/agent/user-memory/nope", headers={"X-User-Id": "u1"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_settings_default_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    main_module._db()
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/agent/user-memory/settings", headers={"X-User-Id": "u1"})
    assert r.status_code == 200
    assert r.json()["enabled"] is True


@pytest.mark.asyncio
async def test_settings_put_then_get_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    main_module._db()
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        p = await ac.put("/agent/user-memory/settings",
                         headers={"X-User-Id": "u1"}, json={"enabled": False})
        assert p.status_code == 200
        assert p.json()["enabled"] is False
        g = await ac.get("/agent/user-memory/settings", headers={"X-User-Id": "u1"})
    assert g.json()["enabled"] is False
    conn = main_module._db()
    row = conn.execute(
        "SELECT value FROM user_settings WHERE user_id='u1' AND key='memory_enabled'"
    ).fetchone()
    assert row["value"] == "0"


@pytest.mark.asyncio
async def test_settings_requires_user_id(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        g = await ac.get("/agent/user-memory/settings")
        p = await ac.put("/agent/user-memory/settings", json={"enabled": True})
    assert g.status_code == 401
    assert p.status_code == 401
