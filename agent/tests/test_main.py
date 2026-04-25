import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch, AsyncMock

@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "test.db"))
    import importlib
    import sys
    # Clean up any cached modules
    for mod in ["main", "agent", "db"]:
        sys.modules.pop(mod, None)
    import main
    importlib.reload(main)
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as c:
        yield c

@pytest.mark.asyncio
async def test_health_returns_ok(client):
    resp = await client.get("/agent/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

@pytest.mark.asyncio
async def test_create_session(client):
    resp = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    assert resp.status_code == 200
    data = resp.json()
    assert "session_id" in data

@pytest.mark.asyncio
async def test_list_sessions(client):
    await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    resp = await client.get("/agent/sessions", headers={"X-User-Id": "1"})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)

@pytest.mark.asyncio
async def test_delete_session(client):
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    session_id = r.json()["session_id"]
    resp = await client.delete(f"/agent/sessions/{session_id}", headers={"X-User-Id": "1"})
    assert resp.status_code == 200

@pytest.mark.asyncio
async def test_run_missing_user_id_returns_401(client):
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    session_id = r.json()["session_id"]
    resp = await client.post(f"/agent/sessions/{session_id}/run", json={"message": "hi"})
    assert resp.status_code == 401

@pytest.mark.asyncio
async def test_confirm_unknown_session_returns_409(client):
    resp = await client.post(
        "/agent/sessions/nonexistent/confirm",
        headers={"X-User-Id": "1"},
    )
    assert resp.status_code == 409
