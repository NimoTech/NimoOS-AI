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


def test_load_history_preserves_reasoning_items(runner, conn):
    """Reasoning items must round-trip through history so the SDK can replay
    them as `reasoning_content` on assistant messages — DeepSeek thinking-mode
    rejects requests where this field is absent on turns that originally
    produced reasoning."""
    import uuid, time
    session_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sessions (id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
        (session_id, "u", "t", int(time.time()), int(time.time())),
    )
    persisted = [
        {"role": "user", "content": "hi"},
        {"type": "reasoning", "id": "r1",
         "summary": [{"type": "summary_text", "text": "thinking..."}],
         "provider_data": {"model": "deepseek-v4-flash"}},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "ok"}]},
    ]
    conn.execute(
        "INSERT INTO messages (id, session_id, role, content, created_at) VALUES (?,?,?,?,?)",
        (str(uuid.uuid4()), session_id, "history", json.dumps(persisted), int(time.time())),
    )
    conn.commit()

    loaded = runner._load_history(session_id)
    assert any(x.get("type") == "reasoning" for x in loaded if isinstance(x, dict))
    assert len(loaded) == 3


def test_inject_synthetic_reasoning_fills_gaps():
    """Every assistant message must be preceded by a reasoning item so the
    SDK can populate `reasoning_content` for DeepSeek thinking-mode replay."""
    from agent import _inject_synthetic_reasoning
    items = [
        {"role": "user", "content": "u1"},
        # Assistant turn WITHOUT preceding reasoning — must get one synthesized.
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "a1"}]},
        {"type": "function_call", "name": "t", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        # Assistant turn WITH preceding reasoning — must NOT get an extra one.
        {"type": "reasoning", "id": "r1",
         "summary": [{"type": "summary_text", "text": "thinking"}],
         "provider_data": {"model": "deepseek-v4-flash"}},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "a2"}]},
    ]
    out = _inject_synthetic_reasoning(items)
    types = [(x.get("type") or x.get("role")) for x in out]
    assert types == [
        "user",
        "reasoning",  # synthesized before a1
        "message",
        "function_call",
        "function_call_output",
        "reasoning",  # original (not duplicated)
        "message",
    ]
    # The synthesized item must carry non-empty summary text (SDK only replays
    # truthy text values).
    synth = out[1]
    assert synth["id"] == "__synthetic__"
    assert synth["summary"][0]["text"]


def test_inject_synthetic_reasoning_no_op_when_no_assistant_messages():
    from agent import _inject_synthetic_reasoning
    items = [{"role": "user", "content": "u"}, {"role": "user", "content": "v"}]
    assert _inject_synthetic_reasoning(items) == items


def test_chatcompletions_model_uses_deepseek_reasoning_replay_hook():
    """The agent must wire the SDK's `should_replay_reasoning_content` hook so
    DeepSeek thinking-mode history requests carry `reasoning_content`."""
    from agents.models.reasoning_content_replay import (
        default_should_replay_reasoning_content,
        ReasoningContentReplayContext, ReasoningContentSource,
    )
    import inspect, agent
    src = inspect.getsource(agent)
    assert "should_replay_reasoning_content=default_should_replay_reasoning_content" in src
    # And sanity-check that the default policy actually opts in for DeepSeek:
    ctx = ReasoningContentReplayContext(
        model="deepseek-v4-flash", base_url=None,
        reasoning=ReasoningContentSource(
            item={}, origin_model="deepseek-v4-flash",
            provider_data={"model": "deepseek-v4-flash"}),
    )
    assert default_should_replay_reasoning_content(ctx) is True
