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
