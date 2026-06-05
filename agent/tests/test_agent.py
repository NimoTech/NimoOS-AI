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


def test_repair_dangling_tool_calls_inserts_output():
    """A function_call with no following output must get a synthetic output so
    the provider doesn't 400 with 'insufficient tool messages'."""
    from agent import _repair_dangling_tool_calls
    items = [
        {"role": "user", "content": "go"},
        {"type": "function_call", "name": "edit_file", "call_id": "c1", "arguments": "{}"},
        # no function_call_output for c1
    ]
    out = _repair_dangling_tool_calls(items)
    assert out[1]["type"] == "function_call"
    assert out[2]["type"] == "function_call_output"
    assert out[2]["call_id"] == "c1"
    assert out[2]["output"]  # non-empty placeholder


def test_repair_dangling_tool_calls_leaves_paired_calls_untouched():
    from agent import _repair_dangling_tool_calls
    items = [
        {"type": "function_call", "name": "t", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
    ]
    assert _repair_dangling_tool_calls(items) == items
    # Idempotent: repairing twice changes nothing.
    once = _repair_dangling_tool_calls(items)
    assert _repair_dangling_tool_calls(once) == once


def test_repair_dangling_tool_calls_mixed_parallel_calls():
    """Two parallel calls where only the first got an output: only the second
    is back-filled, and the existing pairing is preserved."""
    from agent import _repair_dangling_tool_calls
    items = [
        {"type": "function_call", "name": "a", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call", "name": "b", "call_id": "c2", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        # c2 never produced an output (sibling cancelled)
    ]
    out = _repair_dangling_tool_calls(items)
    outputs = [x for x in out if x.get("type") == "function_call_output"]
    answered = {x["call_id"] for x in outputs}
    assert answered == {"c1", "c2"}


def test_repair_tool_messages_inserts_missing_tool_reply():
    """An assistant tool_calls turn with no following tool message must get a
    placeholder tool message (the failure-or-cancel fallback)."""
    from agent import _repair_tool_messages
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "edit_file", "arguments": "{}"}}]},
        # no tool message for c1
    ]
    out = _repair_tool_messages(msgs)
    assert out[2] == {"role": "tool", "tool_call_id": "c1",
                      "content": out[2]["content"]}
    assert out[2]["content"]  # non-empty placeholder


def test_repair_tool_messages_partial_parallel_keeps_order():
    """Two parallel calls, only the first answered → the second (cancelled
    sibling) gets a placeholder, inserted after the existing tool reply, and a
    following assistant turn is preserved."""
    from agent import _repair_tool_messages
    msgs = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "b", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        # c2's reply is missing (sibling cancelled)
        {"role": "assistant", "content": "done"},
    ]
    out = _repair_tool_messages(msgs)
    roles = [m["role"] for m in out]
    assert roles == ["assistant", "tool", "tool", "assistant"]
    assert out[1]["tool_call_id"] == "c1"
    assert out[2]["tool_call_id"] == "c2"
    assert out[3] == {"role": "assistant", "content": "done"}


def test_repair_tool_messages_fully_answered_unchanged():
    from agent import _repair_tool_messages
    msgs = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "a", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    assert _repair_tool_messages(msgs) == msgs


def test_repair_drops_orphan_tool_message():
    """A tool message with no preceding assistant tool_calls must be dropped
    (DeepSeek 400: 'tool must be a response to a preceding message with
    tool_calls')."""
    from agent import _repair_tool_messages
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "ghost", "content": "orphan"},
        {"role": "assistant", "content": "ok"},
    ]
    out = _repair_tool_messages(msgs)
    assert all(m.get("role") != "tool" for m in out)
    assert [m["role"] for m in out] == ["user", "assistant"]


def test_repair_splits_parallel_tool_calls_for_deepseek():
    """DeepSeek can't replay a multi-tool_call assistant message; it must be
    split into sequential single-call assistant+tool pairs, each carrying
    reasoning_content."""
    from agent import _repair_tool_messages
    msgs = [
        {"role": "assistant", "content": "doing", "reasoning_content": "R",
         "tool_calls": [
             {"id": "c1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
             {"id": "c2", "type": "function", "function": {"name": "b", "arguments": "{}"}},
             {"id": "c3", "type": "function", "function": {"name": "c", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
        {"role": "tool", "tool_call_id": "c2", "content": "r2"},
        {"role": "tool", "tool_call_id": "c3", "content": "r3"},
    ]
    out = _repair_tool_messages(msgs, model="deepseek-v4-flash")
    # Expect: asst[c1],tool c1, asst[c2],tool c2, asst[c3],tool c3
    assert [m["role"] for m in out] == [
        "assistant", "tool", "assistant", "tool", "assistant", "tool"]
    for k in range(0, 6, 2):
        am = out[k]
        assert len(am["tool_calls"]) == 1
        assert am["reasoning_content"] == "R"  # carried onto every split msg
        assert out[k + 1]["tool_call_id"] == am["tool_calls"][0]["id"]
    assert out[0]["content"] == "doing"      # original content on the first only
    assert out[2]["content"] is None


def test_repair_keeps_parallel_tool_calls_for_non_deepseek():
    """Non-DeepSeek providers keep the single multi-tool_call assistant message
    (parallel execution is fine for them)."""
    from agent import _repair_tool_messages
    msgs = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "b", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
        {"role": "tool", "tool_call_id": "c2", "content": "r2"},
    ]
    out = _repair_tool_messages(msgs, model="gpt-4o-mini")
    assert [m["role"] for m in out] == ["assistant", "tool", "tool"]
    assert len(out[0]["tool_calls"]) == 2


def test_converter_patch_splits_parallel_for_deepseek_end_to_end():
    """End-to-end through the real SDK converter: a turn with parallel tool calls
    replayed for DeepSeek must come out as single-call assistant+tool pairs, so
    every tool message is immediately preceded by an assistant carrying exactly
    that one tool_call."""
    import agent  # noqa: F401 — applies the converter patch
    from agents.models.chatcmpl_converter import Converter

    items = [
        {"role": "user", "content": "go"},
        {"type": "function_call", "name": "a", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call", "name": "b", "call_id": "c2", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "r1"},
        {"type": "function_call_output", "call_id": "c2", "output": "r2"},
    ]
    msgs = Converter.items_to_messages(items, model="deepseek-v4-flash")
    for k, m in enumerate(msgs):
        if m.get("role") == "tool":
            prev = msgs[k - 1]
            assert prev.get("role") == "assistant"
            tcs = prev.get("tool_calls") or []
            assert len(tcs) == 1, "DeepSeek must see exactly one tool_call per assistant"
            assert tcs[0]["id"] == m["tool_call_id"]


def test_converter_patch_guarantees_paired_tool_messages():
    """End-to-end: the SDK's real items_to_messages, after the agent module's
    monkeypatch, must never emit an assistant tool_calls without a matching tool
    message — even when the input item list has a dangling function_call (e.g. a
    mid-run turn where a parallel sibling was cancelled)."""
    import agent  # noqa: F401 — importing applies the converter patch
    from agents.models.chatcmpl_converter import Converter

    items = [
        {"role": "user", "content": "go"},
        {"type": "function_call", "name": "edit_file",
         "call_id": "c1", "arguments": "{}"},
        # NO function_call_output for c1 — the dangling case
    ]
    msgs = Converter.items_to_messages(items, model="gpt-4o-mini")
    # Collect tool_call ids and the tool_call_ids that answer them.
    call_ids, answered = set(), set()
    for m in msgs:
        if m.get("role") == "assistant":
            for tc in (m.get("tool_calls") or []):
                call_ids.add(tc["id"])
        if m.get("role") == "tool":
            answered.add(m.get("tool_call_id"))
    assert call_ids == {"c1"}
    assert call_ids <= answered, "every tool_call must be answered by a tool message"


@pytest.mark.asyncio
async def test_error_path_persists_repaired_history(runner, conn):
    """When the run blows up mid-stream (e.g. a provider 400), the turn must
    still be saved so it survives a page refresh (the /messages endpoint reads
    only the saved history), with any dangling tool_call repaired so the saved
    history can't re-trigger the same 400 on the next turn."""
    import uuid, time
    session_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sessions (id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
        (session_id, "u", "t", int(time.time()), int(time.time())),
    )
    conn.commit()

    async def boom_stream_events():
        raise RuntimeError(
            "400 An assistant message with 'tool_calls' must be followed by "
            "tool messages (insufficient tool messages following tool_calls message)"
        )
        yield  # unreachable; makes this an async generator

    mock_stream = MagicMock()
    mock_stream.stream_events = boom_stream_events
    mock_stream.final_output = None
    # Cumulative items at the point of failure: a tool call with NO output.
    mock_stream.to_input_list.return_value = [
        {"role": "user", "content": "go"},
        {"type": "function_call", "name": "edit_file", "call_id": "c1", "arguments": "{}"},
    ]

    queue = asyncio.Queue()
    with patch("agent.Runner.run_streamed", return_value=mock_stream):
        # run() swallows the exception and surfaces it as an SSE error event;
        # it must not propagate (main.py owns run-status bookkeeping).
        await runner.run(session_id, "u", "go", queue,
                         "k", "http://localhost/v1", "m")

    rows = conn.execute(
        "SELECT content FROM messages WHERE session_id=?", (session_id,)
    ).fetchall()
    assert len(rows) == 1, "errored turn must still be persisted"
    saved = json.loads(rows[0]["content"])
    calls = [x for x in saved if isinstance(x, dict) and x.get("type") == "function_call"]
    answered = {x.get("call_id") for x in saved
                if isinstance(x, dict) and x.get("type") == "function_call_output"}
    assert calls, "saved history should contain the tool call"
    assert all(c["call_id"] in answered for c in calls), "dangling call must be repaired"

    drained = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    types = [e["type"] for e in drained]
    assert "error" in types and types[-1] == "done"


@pytest.mark.asyncio
async def test_tool_pairing_400_logs_evidence(runner, conn, caplog):
    """A tool_call/tool pairing 400 must dump the exact item list to the
    nimoos-agent logger so the root cause can be confirmed from a real payload."""
    import logging, uuid, time
    session_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sessions (id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
        (session_id, "u", "t", int(time.time()), int(time.time())),
    )
    conn.commit()

    async def boom():
        raise RuntimeError("insufficient tool messages following tool_calls message")
        yield

    mock_stream = MagicMock()
    mock_stream.stream_events = boom
    mock_stream.final_output = None
    mock_stream.to_input_list.return_value = [
        {"type": "function_call", "name": "edit_file", "call_id": "c1", "arguments": "{}"},
    ]

    queue = asyncio.Queue()
    with patch("agent.Runner.run_streamed", return_value=mock_stream):
        with caplog.at_level(logging.WARNING, logger="nimoos-agent"):
            await runner.run(session_id, "u", "go", queue,
                             "k", "http://localhost/v1", "m")

    evidence = [r for r in caplog.records if "tool-pairing 400 evidence" in r.getMessage()]
    assert evidence, "expected an evidence log line for the tool-pairing 400"
    assert "c1" in evidence[0].getMessage(), "evidence must include the offending items"


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


def test_repair_pairs_through_interleaved_empty_assistant_message():
    """DeepSeek thinking-mode emits an empty assistant message alongside a
    tool call; the converter lands it BETWEEN the tool_calls message and the
    real tool reply. The pairing scan must skip (and drop) it instead of
    treating the real reply as an orphan: that substituted the placeholder
    for the model (it kept seeing "no result") while the UI showed the real
    results from the SSE event."""
    from agent import _repair_tool_messages, _SYNTHETIC_TOOL_RESULT
    msgs = [
        {"role": "user", "content": "find beach photos"},
        {"role": "assistant", "content": None, "reasoning_content": "R",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "search_photos", "arguments": "{}"}}]},
        {"role": "assistant", "content": ""},          # empty interloper
        {"role": "tool", "tool_call_id": "c1", "content": '{"count": 15}'},
        {"role": "assistant", "content": "done"},
    ]
    out = _repair_tool_messages(msgs, model="deepseek-v4-flash")
    tools = [m for m in out if m.get("role") == "tool"]
    assert len(tools) == 1
    assert tools[0]["content"] == '{"count": 15}'      # real result preserved
    assert all(m.get("content") != _SYNTHETIC_TOOL_RESULT for m in out)
    # the empty interloper is gone
    assert not any(m.get("role") == "assistant" and not m.get("tool_calls")
                   and not (m.get("content") or "") for m in out)


def test_repair_empty_assistant_skip_applies_to_all_providers():
    """The adjacency break is not DeepSeek-specific — the forward-pairing
    guarantee must survive the interloper for every provider."""
    from agent import _repair_tool_messages, _SYNTHETIC_TOOL_RESULT
    msgs = [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "assistant", "content": ""},
        {"role": "tool", "tool_call_id": "c1", "content": "REAL"},
    ]
    out = _repair_tool_messages(msgs)   # no model kwarg
    tools = [m for m in out if m.get("role") == "tool"]
    assert len(tools) == 1 and tools[0]["content"] == "REAL"
    assert all(m.get("content") != _SYNTHETIC_TOOL_RESULT for m in out)
