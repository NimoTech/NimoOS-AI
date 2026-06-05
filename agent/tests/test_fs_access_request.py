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


async def _resolve_after_event(ctx, confirmed, *, after=0):
    """Wait for the access_request event, then resolve its confirm_id.

    `after`: only consider events past this index (supports callers that need
    to wait for a *new* card after some events have already been emitted).
    """
    for _ in range(200):
        evs = [e for e in ctx["sink"].events if e["type"] == "access_request"]
        if len(evs) > after:
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


def test_waiter_does_not_hang_when_primary_cancelled(ctx):
    # A deduped waiter must not hang if the primary task is cancelled while
    # blocked in mgr.wait(). Requires `except BaseException` so CancelledError
    # propagates to the shared future instead of orphaning it.
    class _BlockingMgr:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
        def register(self, *a):
            return "cid"
        async def wait(self, cid):
            self.started.set()
            await self.release.wait()
            return True
    ctx["confirm_mgr"] = _BlockingMgr()

    async def go():
        primary = asyncio.ensure_future(
            access_request.request_access(ctx, "/DATA/Q", "folder", "list"))
        await ctx["confirm_mgr"].started.wait()      # primary now in mgr.wait()
        waiter = asyncio.ensure_future(
            access_request.request_access(ctx, "/DATA/Q", "folder", "list"))
        await asyncio.sleep(0)                        # let waiter suspend on shared future
        primary.cancel()
        # Must complete promptly; with the bug the waiter hangs and this times out.
        await asyncio.wait_for(
            asyncio.gather(primary, waiter, return_exceptions=True), timeout=1.0)
        return primary.done(), waiter.done()

    pdone, wdone = asyncio.get_event_loop().run_until_complete(go())
    assert pdone and wdone
    assert access_request._pending_requests == {}    # key cleaned up even on cancel


def test_clear_denied_allows_reprompt(ctx):
    async def go():
        t = asyncio.ensure_future(
            access_request.request_access(ctx, "/DATA/Z", "folder", "list"))
        await _resolve_after_event(ctx, False)
        first = await t
        # Simulate a new run/turn: clear denials for the session.
        access_request.clear_denied_for_session(ctx["session_id"])
        t2 = asyncio.ensure_future(
            access_request.request_access(ctx, "/DATA/Z", "folder", "list"))
        await _resolve_after_event(ctx, True, after=1)
        second = await t2
        return first, second
    first, second = asyncio.get_event_loop().run_until_complete(go())
    assert first is False and second is True
    cards = [e for e in ctx["sink"].events if e["type"] == "access_request"]
    assert len(cards) == 2  # second prompt shown after clear


def test_request_persisted_pending_then_granted(ctx):
    async def go():
        task = asyncio.ensure_future(
            access_request.request_access(ctx, "/DATA/Persist", "folder", "list"))
        await asyncio.sleep(0)
        row = ctx["conn"].execute(
            "SELECT decision, run_id, kind FROM access_requests "
            "WHERE session_id='s1' AND path='/DATA/Persist'").fetchone()
        assert row is not None and row["decision"] is None
        assert row["run_id"] == "r1" and row["kind"] == "folder"
        await _resolve_after_event(ctx, True)
        return await task
    granted = asyncio.get_event_loop().run_until_complete(go())
    assert granted is True
    row = ctx["conn"].execute(
        "SELECT decision, resolved_at FROM access_requests WHERE path='/DATA/Persist'").fetchone()
    assert row["decision"] == "granted" and row["resolved_at"] is not None


def test_request_persisted_denied(ctx):
    async def go():
        task = asyncio.ensure_future(
            access_request.request_access(ctx, "/DATA/PersistNo", "folder", "read"))
        await _resolve_after_event(ctx, False)
        return await task
    granted = asyncio.get_event_loop().run_until_complete(go())
    assert granted is False
    row = ctx["conn"].execute(
        "SELECT decision FROM access_requests WHERE path='/DATA/PersistNo'").fetchone()
    assert row["decision"] == "denied"


def test_access_requests_table_persists_across_reinit(tmp_path):
    import db as db_module
    p = str(tmp_path / "persist.db")
    conn = db_module.init_db(p, snapshots_root=str(tmp_path / "s"))
    now = 1000
    conn.execute("INSERT INTO sessions (id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                 ("s1", "u1", None, now, now))
    conn.execute("INSERT INTO access_requests "
                 "(confirm_id,session_id,run_id,path,kind,reason,decision,created_at) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 ("c1", "s1", "r1", "/p", "folder", "x", "granted", now))
    conn.commit()
    conn.close()
    conn2 = db_module.init_db(p, snapshots_root=str(tmp_path / "s"))
    row = conn2.execute("SELECT decision FROM access_requests WHERE confirm_id='c1'").fetchone()
    assert row is not None and row["decision"] == "granted"


def test_batch_approve_persists_all_paths(ctx, tmp_path):
    """request_access_batch emits ONE access_request event; approving it writes
    ALL supplied paths into visible_resources and returns True."""
    dir_a = str(tmp_path / "dirA")
    dir_b = str(tmp_path / "dirB")

    async def go():
        task = asyncio.ensure_future(
            access_request.request_access_batch(ctx, [dir_a, dir_b], "write"))
        await _resolve_after_event(ctx, True)
        return await task

    granted = asyncio.get_event_loop().run_until_complete(go())
    assert granted is True

    # Exactly ONE access_request card emitted for the whole batch.
    cards = [e for e in ctx["sink"].events if e["type"] == "access_request"]
    assert len(cards) == 1, f"expected 1 card, got {len(cards)}"

    # Both directories must now be in visible_resources.
    for path in (dir_a, dir_b):
        row = ctx["conn"].execute(
            "SELECT kind FROM visible_resources WHERE session_id='s1' AND path=?",
            (path,),
        ).fetchone()
        assert row is not None, f"{path!r} missing from visible_resources"


def test_batch_deny_persists_nothing(ctx, tmp_path):
    """Denying a batch request leaves visible_resources untouched and returns False."""
    dir_a = str(tmp_path / "dirX")
    dir_b = str(tmp_path / "dirY")

    async def go():
        task = asyncio.ensure_future(
            access_request.request_access_batch(ctx, [dir_a, dir_b], "read"))
        await _resolve_after_event(ctx, False)
        return await task

    granted = asyncio.get_event_loop().run_until_complete(go())
    assert granted is False

    for path in (dir_a, dir_b):
        row = ctx["conn"].execute(
            "SELECT kind FROM visible_resources WHERE session_id='s1' AND path=?",
            (path,),
        ).fetchone()
        assert row is None, f"{path!r} should NOT be in visible_resources after denial"


def test_batch_empty_list_returns_true(ctx):
    """An empty batch is a no-op and returns True immediately."""
    async def go():
        return await access_request.request_access_batch(ctx, [], "list")

    result = asyncio.get_event_loop().run_until_complete(go())
    assert result is True
    cards = [e for e in ctx["sink"].events if e["type"] == "access_request"]
    assert len(cards) == 0


def test_batch_headless_returns_false(ctx):
    """When confirm_mgr is None (headless mode), batch returns False immediately."""
    ctx["confirm_mgr"] = None

    async def go():
        return await access_request.request_access_batch(ctx, ["/some/path"], "read")

    result = asyncio.get_event_loop().run_until_complete(go())
    assert result is False


def test_cancelled_request_recorded_as_cancelled(ctx):
    class _BlockingMgr:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
        def register(self, *a):
            return "cidX"
        async def wait(self, cid):
            self.started.set()
            await self.release.wait()
            return True
    ctx["confirm_mgr"] = _BlockingMgr()

    async def go():
        task = asyncio.ensure_future(
            access_request.request_access(ctx, "/DATA/Cancel", "folder", "list"))
        await ctx["confirm_mgr"].started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.get_event_loop().run_until_complete(go())
    row = ctx["conn"].execute(
        "SELECT decision FROM access_requests WHERE confirm_id='cidX'").fetchone()
    assert row is not None and row["decision"] == "cancelled"
