import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "test.db"))
    import importlib, sys
    for mod in ["main", "agent", "db", "title_gen"]:
        sys.modules.pop(mod, None)
    import main
    importlib.reload(main)
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as c:
        yield c

@pytest.mark.asyncio
async def test_patch_title_happy_path(client):
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    sid = r.json()["session_id"]
    resp = await client.patch(
        f"/agent/sessions/{sid}/title",
        headers={"X-User-Id": "1"},
        json={"title": "Project Alpha"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Project Alpha"
    assert "updated_at" in body

    listed = await client.get("/agent/sessions", headers={"X-User-Id": "1"})
    titles = [s["title"] for s in listed.json()]
    assert "Project Alpha" in titles


@pytest.mark.asyncio
async def test_patch_title_empty_rejected(client):
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    sid = r.json()["session_id"]
    resp = await client.patch(
        f"/agent/sessions/{sid}/title",
        headers={"X-User-Id": "1"},
        json={"title": "   "},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_patch_title_wrong_user_404(client):
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    sid = r.json()["session_id"]
    resp = await client.patch(
        f"/agent/sessions/{sid}/title",
        headers={"X-User-Id": "2"},
        json={"title": "stolen"},
    )
    assert resp.status_code == 404
