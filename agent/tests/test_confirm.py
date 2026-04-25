import asyncio
import sqlite3
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
    async def confirm_after_delay():
        await asyncio.sleep(0.05)
        mgr.resolve("session-1", confirmed=True)
    asyncio.create_task(confirm_after_delay())
    result = await mgr.wait("session-1", "stop_app", "Stop Plex?", "nimoos-cli app-management stop 1")
    assert result is True

@pytest.mark.asyncio
async def test_confirm_resolves_false(db):
    mgr = ConfirmManager(db)
    async def cancel_after_delay():
        await asyncio.sleep(0.05)
        mgr.resolve("session-2", confirmed=False)
    asyncio.create_task(cancel_after_delay())
    result = await mgr.wait("session-2", "stop_app", "Stop Plex?", "nimoos-cli app-management stop 1")
    assert result is False

@pytest.mark.asyncio
async def test_confirm_times_out(db):
    mgr = ConfirmManager(db, timeout=0.1)
    result = await mgr.wait("session-3", "stop_app", "Stop?", "cmd")
    assert result is False  # timeout → auto-cancel

@pytest.mark.asyncio
async def test_resolve_unknown_session_raises(db):
    mgr = ConfirmManager(db)
    with pytest.raises(KeyError, match="session_expired"):
        mgr.resolve("nonexistent", confirmed=True)

@pytest.mark.asyncio
async def test_pending_written_to_db(db):
    mgr = ConfirmManager(db, timeout=0.05)
    # Start wait (will timeout quickly), check DB before it clears
    task = asyncio.create_task(
        mgr.wait("session-4", "stop_app", "Stop?", "cmd")
    )
    await asyncio.sleep(0.01)
    row = db.execute("SELECT * FROM pending_confirmations WHERE session_id=?", ("session-4",)).fetchone()
    assert row is not None
    await task
    # After timeout, DB entry is cleaned up
    row = db.execute("SELECT * FROM pending_confirmations WHERE session_id=?", ("session-4",)).fetchone()
    assert row is None
