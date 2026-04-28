import asyncio
import pytest
from confirm import ConfirmManager


@pytest.fixture
def db(tmp_path):
    import db as db_module
    conn = db_module.init_db(str(tmp_path / "test.db"))
    return conn


@pytest.mark.asyncio
async def test_confirm_resolves_true(db):
    mgr = ConfirmManager(db)
    confirm_id = mgr.register("session-1", "stop_app", "Stop Plex?", "cmd")

    async def confirm_after_delay():
        await asyncio.sleep(0.05)
        mgr.resolve(confirm_id, confirmed=True)

    asyncio.create_task(confirm_after_delay())
    assert await mgr.wait(confirm_id) is True


@pytest.mark.asyncio
async def test_confirm_resolves_false(db):
    mgr = ConfirmManager(db)
    confirm_id = mgr.register("session-2", "stop_app", "Stop?", "cmd")

    async def cancel_after_delay():
        await asyncio.sleep(0.05)
        mgr.resolve(confirm_id, confirmed=False)

    asyncio.create_task(cancel_after_delay())
    assert await mgr.wait(confirm_id) is False


@pytest.mark.asyncio
async def test_confirm_times_out(db):
    mgr = ConfirmManager(db, timeout=0.1)
    confirm_id = mgr.register("session-3", "stop_app", "Stop?", "cmd")
    assert await mgr.wait(confirm_id) is False  # timeout → auto-cancel


@pytest.mark.asyncio
async def test_resolve_unknown_confirm_raises(db):
    mgr = ConfirmManager(db)
    with pytest.raises(KeyError, match="confirm_expired"):
        mgr.resolve("nonexistent", confirmed=True)


@pytest.mark.asyncio
async def test_resolve_session_mismatch_raises(db):
    mgr = ConfirmManager(db)
    confirm_id = mgr.register("session-A", "stop_app", "?", "cmd")
    with pytest.raises(KeyError, match="confirm_session_mismatch"):
        mgr.resolve(confirm_id, confirmed=True, expected_session_id="session-B")


@pytest.mark.asyncio
async def test_two_confirms_in_one_session_are_independent(db):
    mgr = ConfirmManager(db)
    cid_a = mgr.register("session-X", "stop_app", "Stop A?", "cmd a")
    cid_b = mgr.register("session-X", "stop_app", "Stop B?", "cmd b")

    async def resolve_only_b():
        await asyncio.sleep(0.05)
        mgr.resolve(cid_b, confirmed=True)

    asyncio.create_task(resolve_only_b())
    # Wait on B — it should resolve quickly with True.
    assert await asyncio.wait_for(mgr.wait(cid_b), timeout=1.0) is True
    # A is still waiting; resolve it now to keep the test bounded.
    mgr.resolve(cid_a, confirmed=False)
    assert await mgr.wait(cid_a) is False


@pytest.mark.asyncio
async def test_cancel_session_rejects_all_in_session(db):
    mgr = ConfirmManager(db)
    cid_a = mgr.register("session-Y", "a", "?", "cmd")
    cid_b = mgr.register("session-Y", "b", "?", "cmd")
    cid_other = mgr.register("session-Z", "c", "?", "cmd")

    n = mgr.cancel_session("session-Y")
    assert n == 2

    assert await asyncio.wait_for(mgr.wait(cid_a), timeout=1.0) is False
    assert await asyncio.wait_for(mgr.wait(cid_b), timeout=1.0) is False
    # The other session's confirmation is untouched. Resolve it to avoid the
    # event loop leaking the wait task in the test runner.
    mgr.resolve(cid_other, confirmed=True)
    assert await mgr.wait(cid_other) is True


@pytest.mark.asyncio
async def test_pending_written_to_db(db):
    mgr = ConfirmManager(db, timeout=0.05)
    confirm_id = mgr.register("session-4", "stop_app", "Stop?", "cmd")
    row = db.execute(
        "SELECT * FROM pending_confirmations WHERE confirm_id=?",
        (confirm_id,),
    ).fetchone()
    assert row is not None
    assert row["session_id"] == "session-4"
    # Wait will time out and cleanup
    await mgr.wait(confirm_id)
    row = db.execute(
        "SELECT * FROM pending_confirmations WHERE confirm_id=?",
        (confirm_id,),
    ).fetchone()
    assert row is None
