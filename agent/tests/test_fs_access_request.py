import asyncio
import time
import pytest

import db as db_module
from confirm import ConfirmManager
from fs import access_request


class FakeSink:
    def __init__(self):
        self.events = []
    async def put(self, e):
        self.events.append(e)


@pytest.fixture(autouse=True)
def _clean_state():
    access_request.reset_state()
    yield
    access_request.reset_state()


@pytest.fixture
def ctx(tmp_path):
    conn = db_module.init_db(str(tmp_path / "a.db"),
                             snapshots_root=str(tmp_path / "snap"))
    now = int(time.time())
    conn.execute("INSERT INTO sessions (id, user_id, title, created_at, updated_at) "
                 "VALUES (?,?,?,?,?)", ("s1", "u1", None, now, now))
    conn.commit()
    return {
        "conn": conn, "sink": FakeSink(), "session_id": "s1",
        "run_id": "r1", "user_patterns": [],
        "confirm_mgr": ConfirmManager(conn),
    }


async def _resolve_after_event(ctx, confirmed):
    """Wait for the access_request event, then resolve its confirm_id."""
    for _ in range(200):
        evs = [e for e in ctx["sink"].events if e["type"] == "access_request"]
        if evs:
            ctx["confirm_mgr"].resolve(evs[-1]["confirm_id"], confirmed,
                                       expected_session_id="s1")
            return
        await asyncio.sleep(0.005)
    raise AssertionError("no access_request event emitted")


def test_grant_writes_visible_resource_and_emits_event(ctx):
    async def go():
        task = asyncio.ensure_future(
            access_request.request_access(ctx, "/DATA/Documents", "folder", "list"))
        await _resolve_after_event(ctx, True)
        return await task
    granted = asyncio.get_event_loop().run_until_complete(go())
    assert granted is True
    ev = [e for e in ctx["sink"].events if e["type"] == "access_request"][0]
    assert ev["path"] == "/DATA/Documents" and ev["kind"] == "folder"
    assert ev["reason"] == "需要浏览该文件夹"
    row = ctx["conn"].execute(
        "SELECT kind FROM visible_resources WHERE session_id='s1' AND path='/DATA/Documents'"
    ).fetchone()
    assert row is not None and row["kind"] == "folder"


def test_deny_does_not_write_and_remembers(ctx):
    async def go():
        task = asyncio.ensure_future(
            access_request.request_access(ctx, "/DATA/Secret", "folder", "read"))
        await _resolve_after_event(ctx, False)
        first = await task
        # second attempt: short-circuits, NO new card
        second = await access_request.request_access(ctx, "/DATA/Secret", "folder", "read")
        return first, second
    first, second = asyncio.get_event_loop().run_until_complete(go())
    assert first is False and second is False
    cards = [e for e in ctx["sink"].events if e["type"] == "access_request"]
    assert len(cards) == 1  # denied path not re-prompted
    assert ctx["conn"].execute(
        "SELECT COUNT(*) c FROM visible_resources WHERE path='/DATA/Secret'"
    ).fetchone()["c"] == 0


def test_concurrent_dedupe_single_card(ctx):
    async def go():
        t1 = asyncio.ensure_future(
            access_request.request_access(ctx, "/DATA/Shared", "folder", "list"))
        t2 = asyncio.ensure_future(
            access_request.request_access(ctx, "/DATA/Shared", "folder", "list"))
        await _resolve_after_event(ctx, True)
        return await asyncio.gather(t1, t2)
    r1, r2 = asyncio.get_event_loop().run_until_complete(go())
    assert r1 is True and r2 is True
    cards = [e for e in ctx["sink"].events if e["type"] == "access_request"]
    assert len(cards) == 1  # only one card despite two concurrent callers


def test_pending_map_cleared_after_resolution(ctx):
    async def go():
        task = asyncio.ensure_future(
            access_request.request_access(ctx, "/DATA/X", "folder", "list"))
        await _resolve_after_event(ctx, True)
        await task
    asyncio.get_event_loop().run_until_complete(go())
    assert access_request._pending_requests == {}  # finally cleaned up
