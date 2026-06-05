import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "test.db"))
    import importlib
    import sys
    for mod in ["main", "agent", "db"]:
        sys.modules.pop(mod, None)
    import main
    importlib.reload(main)
    async with AsyncClient(transport=ASGITransport(app=main.app),
                           base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_create_session_defaults_to_general(client):
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    assert r.status_code == 200
    assert r.json()["agent_type"] == "general"


@pytest.mark.asyncio
async def test_create_photos_session_and_list_returns_agent_type(client):
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"},
                          json={"agent_type": "photos"})
    assert r.status_code == 200
    assert r.json()["agent_type"] == "photos"
    sid = r.json()["session_id"]

    lst = await client.get("/agent/sessions", headers={"X-User-Id": "1"})
    row = next(s for s in lst.json() if s["id"] == sid)
    assert row["agent_type"] == "photos"


@pytest.mark.asyncio
async def test_create_unknown_agent_type_422(client):
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"},
                          json={"agent_type": "hacker"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_photos_session_rejects_visible_resources(client):
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"},
                          json={"agent_type": "photos"})
    sid = r.json()["session_id"]
    resp = await client.post(
        f"/agent/sessions/{sid}/visible-resources",
        headers={"X-User-Id": "1"},
        json={"path": "/tmp", "kind": "folder"},
    )
    assert resp.status_code == 400
    assert "profile" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_photos_session_rejects_kind_init(client):
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"},
                          json={"agent_type": "photos"})
    sid = r.json()["session_id"]
    resp = await client.post(
        f"/agent/sessions/{sid}/run",
        headers={"X-User-Id": "1",
                 "X-Agent-Provider-Key": "k",
                 "X-Agent-Provider-Url": "http://x"},
        json={"message": "go", "kind": "init", "init_target": "/tmp"},
    )
    assert resp.status_code == 400
    assert "profile" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_general_session_visible_resources_still_works(client):
    # Regression guard: the new gate must not break the general path.
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    sid = r.json()["session_id"]
    resp = await client.post(
        f"/agent/sessions/{sid}/visible-resources",
        headers={"X-User-Id": "1"},
        json={"path": "/tmp", "kind": "folder"},
    )
    # /tmp exists in the test environment; anything but the profile-400 means
    # the gate let the request through to the original logic.
    assert resp.status_code != 400 or "profile" not in resp.json().get("detail", "")
