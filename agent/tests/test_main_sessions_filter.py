"""GET /agent/sessions: ?agent_type= filter and the ask_turns first-question title fallback.

Both exist for the knowledge-ask page: its 'search' sessions used to land in the agent
chat sidebar as "Untitled", and it had no way to list its own history.
"""
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

H = {"X-User-Id": "1"}


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


async def _create(c, agent_type=None):
    body = {"agent_type": agent_type} if agent_type else None
    r = await c.post("/agent/sessions", headers=H, json=body)
    assert r.status_code == 200
    return r.json()["session_id"]


@pytest.mark.asyncio
async def test_filter_by_agent_type(client):
    c, _ = client
    gen = await _create(c)
    ask = await _create(c, "search")
    photos = await _create(c, "photos")

    everything = {s["id"] for s in (await c.get("/agent/sessions", headers=H)).json()}
    assert everything == {gen, ask, photos}

    only_general = (await c.get("/agent/sessions", params={"agent_type": "general"}, headers=H)).json()
    assert [s["id"] for s in only_general] == [gen]
    assert all(s["agent_type"] == "general" for s in only_general)

    only_ask = (await c.get("/agent/sessions", params={"agent_type": "search"}, headers=H)).json()
    assert [s["id"] for s in only_ask] == [ask]


@pytest.mark.asyncio
async def test_unknown_agent_type_is_422_with_fixed_message(client):
    c, _ = client
    r = await c.get("/agent/sessions", params={"agent_type": "nope"}, headers=H)
    assert r.status_code == 422
    assert r.json()["detail"] == "invalid agent_type"
    assert "nope" not in r.text


@pytest.mark.asyncio
async def test_untitled_ask_session_takes_its_first_question_as_title(client):
    c, main = client
    sid = await _create(c, "search")
    from ask import store
    store.insert_turn(main._conn, session_id=sid, run_id="r1", question="first question",
                      plan={}, sources=[], stages=[])
    store.insert_turn(main._conn, session_id=sid, run_id="r2", question="follow-up",
                      plan={}, sources=[], stages=[])
    rows = (await c.get("/agent/sessions", params={"agent_type": "search"}, headers=H)).json()
    assert len(rows) == 1  # a correlated subquery, not a join: N turns still = 1 row
    assert rows[0]["title"] == "first question"


@pytest.mark.asyncio
async def test_real_title_wins_over_ask_turns_and_untitled_stays_null(client):
    c, main = client
    sid = await _create(c, "search")
    from ask import store
    store.insert_turn(main._conn, session_id=sid, run_id="r", question="q", plan={}, sources=[], stages=[])
    main._conn.execute("UPDATE sessions SET title=? WHERE id=?", ("My title", sid))
    main._conn.commit()
    empty = await _create(c)  # no turns, no title
    rows = {s["id"]: s for s in (await c.get("/agent/sessions", headers=H)).json()}
    assert rows[sid]["title"] == "My title"
    assert rows[empty]["title"] is None


@pytest.mark.asyncio
async def test_other_users_ask_turns_never_leak_into_title(client):
    c, main = client
    mine = await _create(c, "search")
    theirs = (await c.post("/agent/sessions", headers={"X-User-Id": "2"}, json={"agent_type": "search"})).json()["session_id"]
    from ask import store
    store.insert_turn(main._conn, session_id=theirs, run_id="r", question="their secret", plan={}, sources=[], stages=[])
    rows = (await c.get("/agent/sessions", headers=H)).json()
    assert [s["id"] for s in rows] == [mine]
    assert rows[0]["title"] is None
