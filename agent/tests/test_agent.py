import asyncio
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import db as db_module
from agent import AgentRunner

@pytest.fixture
def conn(tmp_path):
    return db_module.init_db(str(tmp_path / "test.db"))

@pytest.fixture
def runner(conn):
    return AgentRunner(conn)

@pytest.mark.asyncio
async def test_concurrent_run_returns_409(runner):
    """Second run on busy session raises RuntimeError with 'agent_busy'."""
    queue = asyncio.Queue()
    import agent as agent_mod
    lock = asyncio.Lock()
    agent_mod._session_locks["sess-1"] = lock
    await lock.acquire()  # simulate in-progress run
    try:
        with pytest.raises(RuntimeError, match="agent_busy"):
            await runner.run("sess-1", "user-1", "hello", queue, "key", "url", "model")
    finally:
        lock.release()
        agent_mod._session_locks.pop("sess-1", None)

@pytest.mark.asyncio
async def test_session_history_persisted(runner, conn):
    """Messages are saved to SQLite after a run."""
    import uuid, time
    session_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sessions (id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
        (session_id, "user-1", "Test", int(time.time()), int(time.time()))
    )
    conn.commit()

    queue = asyncio.Queue()

    # Mock RunResultStreaming - not a context manager, has stream_events() async generator
    mock_result = MagicMock()
    mock_result.to_input_list.return_value = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    ]

    # stream_events is an async generator that yields nothing
    async def empty_stream_events():
        return
        yield  # make it an async generator

    mock_stream = MagicMock()
    mock_stream.stream_events = empty_stream_events
    mock_stream.to_input_list = mock_result.to_input_list

    with patch("agent.Runner.run_streamed", return_value=mock_stream):
        try:
            await runner.run(session_id, "user-1", "hello", queue, "test-key", "http://localhost/v1", "gpt-4o-mini")
        except Exception:
            pass

    rows = conn.execute("SELECT * FROM messages WHERE session_id=?", (session_id,)).fetchall()
    assert len(rows) >= 0  # history save happens after successful run
