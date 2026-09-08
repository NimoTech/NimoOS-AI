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
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as c:
        yield c, main


@pytest.mark.asyncio
async def test_create_search_session(client):
    c, _ = client
    r = await c.post("/agent/sessions", headers={"X-User-Id": "1"}, json={"agent_type": "search"})
    assert r.status_code == 200 and r.json()["agent_type"] == "search"


@pytest.mark.asyncio
async def test_ask_turns_lists_only_owner_rows(client):
    c, main = client
    sid = (await c.post("/agent/sessions", headers={"X-User-Id": "1"}, json={"agent_type": "search"})).json()["session_id"]
    from ask import store
    store.insert_turn(main._conn, session_id=sid, run_id="r", question="q", plan={"intent": "lookup"},
                      sources=[{"n": 1}], stages=[])
    r = await c.get(f"/agent/sessions/{sid}/ask-turns", headers={"X-User-Id": "1"})
    assert r.status_code == 200 and r.json()[0]["question"] == "q" and r.json()[0]["sources"] == [{"n": 1}]
    r2 = await c.get(f"/agent/sessions/{sid}/ask-turns", headers={"X-User-Id": "2"})
    assert r2.status_code == 404
