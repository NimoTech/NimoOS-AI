import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import run_context as rc
import tool_output as to
from skills import orchestration as orch


class _Sink:
    def __init__(self): self.events = []
    async def put(self, e): self.events.append(e)


def _tool(name):
    return SimpleNamespace(name=name, on_invoke_tool=None)


def _parent(sink, tmp_path, **kw):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    ctx = rc.RunCtx(session_id="s1", user_id="u1", model_name="m", provider_type="other",
                    window=8000, sink=sink, summarize_fn=None, **kw)
    ctx.extra["base_instructions"] = "PARENT SYS"
    rc.RUN_CTX_VAR.set(ctx)
    agent = SimpleNamespace(
        tools=[_tool(n) for n in ("read_file", "web_fetch", "delegate", "update_plan", "remember",
                                  "forget", "create_scheduled_task", "update_task_prompt")],
        model="MODEL", model_settings="MS")
    import mcp_client.client as mcp_client
    mcp_client.RUN_AGENT_VAR.set(agent)
    return ctx, agent


def _fake_stream(events, final="child final answer", peak=1234):
    """A RunResultStreaming stand-in: yields pre-converted SDK-ish events."""
    from agents.stream_events import RunItemStreamEvent
    class _Item:
        def __init__(self, t, **kw): self.type = t; self.__dict__.update(kw)
    async def _events():
        for e in events:
            yield e
    m = SimpleNamespace()
    m.stream_events = _events
    m.final_output = final
    m.cancel = lambda *a, **k: None
    return m


@pytest.fixture(autouse=True)
def _reset():
    yield
    rc.RUN_CTX_VAR.set(None)


def test_registered_as_core_and_excluded_set():
    from skills import ALL_TOOLS
    from skills import tool_registry as reg
    assert "delegate" in {getattr(t, "name", "") for t in ALL_TOOLS}
    assert "delegate" in reg.CORE_TOOL_NAMES
    assert orch.DELEGATE_EXCLUDED_TOOLS == frozenset({"delegate", "update_plan", "remember", "forget",
                                                       "create_scheduled_task", "update_task_prompt"})


@pytest.mark.asyncio
async def test_delegate_builds_lean_agent_and_wraps_events(tmp_path):
    sink = _Sink()
    parent_ctx, parent_agent = _parent(sink, tmp_path)
    captured = {}

    def fake_run_streamed(agent, input_messages, **kwargs):
        captured["agent"] = agent
        captured["kwargs"] = kwargs
        captured["input"] = input_messages
        captured["child_ctx"] = rc.current()
        return _fake_stream([])

    with patch("skills.orchestration.Runner.run_streamed", side_effect=fake_run_streamed), \
         patch("skills.orchestration._convert_event", side_effect=lambda e, n, s: e):
        to.CALL_ID_VAR.set("call_p1")
        out = await orch._delegate_impl("Collect Beelink news", "feeds: a, b", "5 bullets", 7)

    child = captured["agent"]
    assert {t.name for t in child.tools} == {"read_file", "web_fetch"}
    assert child.model == "MODEL" and child.model_settings == "MS"
    assert "Collect Beelink news" in child.instructions and "5 bullets" in child.instructions
    assert "PARENT SYS" not in child.instructions
    assert captured["kwargs"]["max_turns"] == 7
    assert captured["input"] == [{"role": "user", "content": orch._child_user_message("Collect Beelink news", "feeds: a, b", "5 bullets")}]
    cctx = captured["child_ctx"]
    assert cctx is not parent_ctx and cctx.depth == 1 and cctx.conn is None and cctx.parent_call_id == "call_p1"
    assert cctx.sink is not None and cctx.extra["base_instructions"] == child.instructions
    assert rc.current() is parent_ctx                      # parent var untouched
    assert out == "child final answer"
    types = [e["type"] for e in sink.events]
    assert types[0] == "subagent_start" and types[-1] == "subagent_end"
    assert sink.events[0] == {"type": "subagent_start", "call_id": "call_p1", "goal": "Collect Beelink news"}
    assert sink.events[-1]["status"] == "ok" and sink.events[-1]["call_id"] == "call_p1"


@pytest.mark.asyncio
async def test_delegate_forwards_converted_events_wrapped(tmp_path):
    sink = _Sink()
    _parent(sink, tmp_path)
    inner = [{"type": "tool_call", "tool": "web_fetch", "args": {}, "call_id": "c1"},
             {"type": "message_delta", "content": "hi"}]
    with patch("skills.orchestration.Runner.run_streamed", return_value=_fake_stream(inner)), \
         patch("skills.orchestration._convert_event", side_effect=lambda e, n, s: e):
        to.CALL_ID_VAR.set("call_p2")
        await orch._delegate_impl("g", "", "", 15)
    wrapped = [e for e in sink.events if e["type"] == "subagent_event"]
    assert [w["event"] for w in wrapped] == inner
    assert all(w["parent_call_id"] == "call_p2" and w["depth"] == 1 for w in wrapped)


@pytest.mark.asyncio
async def test_delegate_refused_at_depth_1_and_without_parent(tmp_path):
    sink = _Sink()
    _parent(sink, tmp_path, depth=1)
    out = await orch._delegate_impl("g", "", "", 15)
    assert out.startswith("[delegate failed: nested delegation is not allowed")
    rc.RUN_CTX_VAR.set(None)
    assert (await orch._delegate_impl("g", "", "", 15)).startswith("[delegate failed: no run context")


@pytest.mark.asyncio
async def test_delegate_clamps_turns_and_offloads_long_result(tmp_path):
    sink = _Sink()
    _parent(sink, tmp_path)
    seen = {}
    def fake(agent, input_messages, **kw):
        seen["max_turns"] = kw["max_turns"]
        return _fake_stream([], final="y" * 5000)
    with patch("skills.orchestration.Runner.run_streamed", side_effect=fake), \
         patch("skills.orchestration._convert_event", side_effect=lambda e, n, s: e):
        to.CALL_ID_VAR.set("call_p3")
        out = await orch._delegate_impl("g", "", "", 99)
    assert seen["max_turns"] == orch.DELEGATE_MAX_TURNS
    assert to.TRAILER_RE.search(out) and "chars=5000" in out
    assert (tmp_path / "call_p3.txt").read_text() == "y" * 5000


@pytest.mark.asyncio
async def test_delegate_timeout_returns_partial_never_raises(tmp_path, monkeypatch):
    sink = _Sink()
    _parent(sink, tmp_path)
    monkeypatch.setattr(orch, "DELEGATE_TIMEOUT", 0.05)
    async def _slow():
        yield {"type": "message_delta", "content": "partial text"}
        await asyncio.sleep(1)
        yield {"type": "message_delta", "content": "never"}
    m = SimpleNamespace(stream_events=_slow, final_output=None, cancel=lambda *a, **k: None)
    with patch("skills.orchestration.Runner.run_streamed", return_value=m), \
         patch("skills.orchestration._convert_event", side_effect=lambda e, n, s: e):
        to.CALL_ID_VAR.set("call_p4")
        out = await orch._delegate_impl("g", "", "", 15)
    assert out.startswith("[delegate failed: timeout after") and "partial: partial text" in out
    assert sink.events[-1]["type"] == "subagent_end" and sink.events[-1]["status"] == "timeout"


@pytest.mark.asyncio
async def test_delegate_exception_returns_failure_string(tmp_path):
    sink = _Sink()
    _parent(sink, tmp_path)
    with patch("skills.orchestration.Runner.run_streamed", side_effect=RuntimeError("boom")):
        to.CALL_ID_VAR.set("call_p5")
        out = await orch._delegate_impl("g", "", "", 15)
    assert out.startswith("[delegate failed: RuntimeError: boom")
    assert sink.events[-1]["status"] == "error"


@pytest.mark.asyncio
async def test_delegate_uses_final_output_or_streamed_text(tmp_path):
    sink = _Sink()
    _parent(sink, tmp_path)
    inner = [{"type": "message_delta", "content": "a"}, {"type": "message_delta", "content": "b"}]
    with patch("skills.orchestration.Runner.run_streamed", return_value=_fake_stream(inner, final=None)), \
         patch("skills.orchestration._convert_event", side_effect=lambda e, n, s: e):
        to.CALL_ID_VAR.set("call_p6")
        assert await orch._delegate_impl("g", "", "", 15) == "ab"


@pytest.mark.asyncio
async def test_delegate_suppresses_duplicate_consolidated_message(tmp_path):
    # Chat-completions-shaped providers (DeepSeek, Ollama, OpenAI-compatible)
    # stream message_delta pieces AND then emit one consolidated "message"
    # item for the same text. That consolidated item must not be forwarded
    # to the nested event stream (it would render the answer twice in the
    # SubagentCard/transcript) nor double-counted in the returned text.
    sink = _Sink()
    _parent(sink, tmp_path)
    inner = [{"type": "message_delta", "content": "he"},
             {"type": "message_delta", "content": "llo"},
             {"type": "message", "content": "hello"}]

    def _convert(e, call_names, state):
        if e.get("type") == "message_delta":
            state["streamed_message"] = True
        return e

    with patch("skills.orchestration.Runner.run_streamed", return_value=_fake_stream(inner, final=None)), \
         patch("skills.orchestration._convert_event", side_effect=_convert):
        to.CALL_ID_VAR.set("call_p7")
        out = await orch._delegate_impl("g", "", "", 15)
    assert out == "hello"                      # answer appears once in the returned text
    wrapped = [e for e in sink.events if e["type"] == "subagent_event"]
    forwarded_types = [w["event"]["type"] for w in wrapped]
    assert forwarded_types == ["message_delta", "message_delta"]   # consolidated "message" NOT forwarded


@pytest.mark.asyncio
async def test_delegate_cancel_mid_stream_emits_subagent_end_and_propagates(tmp_path):
    sink = _Sink()
    _parent(sink, tmp_path)
    started = asyncio.Event()

    async def _slow():
        yield {"type": "message_delta", "content": "partial"}
        started.set()
        await asyncio.sleep(10)
        yield {"type": "message_delta", "content": "never"}

    m = SimpleNamespace(stream_events=_slow, final_output=None, cancel=lambda *a, **k: None)
    with patch("skills.orchestration.Runner.run_streamed", return_value=m), \
         patch("skills.orchestration._convert_event", side_effect=lambda e, n, s: e):
        to.CALL_ID_VAR.set("call_p8")
        task = asyncio.ensure_future(orch._delegate_impl("g", "", "", 15))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert sink.events[-1]["type"] == "subagent_end" and sink.events[-1]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_child_expand_tools_does_not_persist_to_parent_session(tmp_path, monkeypatch):
    # Minor 1: a child's in-run expand_tools() must not durably widen the
    # PARENT session's unlocked-tool set. run_subagent sets
    # GATING_SESSION_VAR to "" for the child, which makes tool_gating._persist
    # a no-op — spy on db.set_unlocked_categories (the actual write) to prove
    # it is never called.
    import db
    import skills.tool_gating as tg
    persisted = []
    monkeypatch.setattr(db, "set_unlocked_categories", lambda *a, **kw: persisted.append((a, kw)))
    sink = _Sink()
    _parent(sink, tmp_path)
    parent_gate_token = tg.GATING_SESSION_VAR.set("s1")
    try:
        def fake_run_streamed(agent, input_messages, **kwargs):
            # Simulate the child model calling expand_tools mid-run.
            tg.expand_categories(["apps"])
            return _fake_stream([])
        with patch("skills.orchestration.Runner.run_streamed", side_effect=fake_run_streamed), \
             patch("skills.orchestration._convert_event", side_effect=lambda e, n, s: e):
            to.CALL_ID_VAR.set("call_gate1")
            await orch._delegate_impl("g", "", "", 5)
        assert persisted == []                                  # never written to the DB
        assert tg.GATING_SESSION_VAR.get("") == "s1"             # parent's var restored after the child
    finally:
        tg.GATING_SESSION_VAR.reset(parent_gate_token)


@pytest.mark.asyncio
async def test_reasoning_only_child_forwards_synthetic_message_for_card(tmp_path):
    # Minor 2: when the child streams no message/message_delta events at all
    # (only tool calls, say) but final_output holds the real answer, forward
    # one synthetic {"type": "message", ...} through the wrap so the
    # SubagentCard isn't left empty.
    sink = _Sink()
    _parent(sink, tmp_path)
    inner = [{"type": "tool_call", "tool": "read_file", "args": {}, "call_id": "c1"}]
    with patch("skills.orchestration.Runner.run_streamed",
              return_value=_fake_stream(inner, final="reasoning answer")), \
         patch("skills.orchestration._convert_event", side_effect=lambda e, n, s: e):
        to.CALL_ID_VAR.set("call_p9")
        out = await orch._delegate_impl("g", "", "", 15)
    assert out == "reasoning answer"
    wrapped = [e["event"] for e in sink.events if e["type"] == "subagent_event"]
    assert wrapped[0]["type"] == "tool_call"
    assert wrapped[-1] == {"type": "message", "content": "reasoning answer"}


@pytest.mark.asyncio
async def test_streamed_child_does_not_get_a_duplicate_synthetic_message(tmp_path):
    # The flip side of the above: a child that DID stream a message must not
    # also get the synthetic one appended.
    sink = _Sink()
    _parent(sink, tmp_path)
    inner = [{"type": "message_delta", "content": "hi"}]
    with patch("skills.orchestration.Runner.run_streamed",
              return_value=_fake_stream(inner, final="hi")), \
         patch("skills.orchestration._convert_event", side_effect=lambda e, n, s: e):
        to.CALL_ID_VAR.set("call_p10")
        out = await orch._delegate_impl("g", "", "", 15)
    assert out == "hi"
    wrapped = [e["event"] for e in sink.events if e["type"] == "subagent_event"]
    assert wrapped == inner                                      # no extra synthetic message


def test_sem_by_loop_is_weak_keyed_and_reused_per_loop():
    # Minor 3: keyed on the loop OBJECT via a WeakKeyDictionary, not id(loop)
    # in a plain dict — two distinct loops get two distinct semaphores, the
    # same loop reuses the same one, and dead loops don't linger.
    import weakref
    assert isinstance(orch._SEM_BY_LOOP, weakref.WeakKeyDictionary)
    orch._SEM_BY_LOOP.clear()

    async def _get():
        return orch._sem()

    loop1 = asyncio.new_event_loop()
    loop2 = asyncio.new_event_loop()
    try:
        sem1a = loop1.run_until_complete(_get())
        sem1b = loop1.run_until_complete(_get())
        sem2 = loop2.run_until_complete(_get())
        assert sem1a is sem1b
        assert sem1a is not sem2
        assert len(orch._SEM_BY_LOOP) == 2
    finally:
        loop1.close()
        loop2.close()


@pytest.mark.asyncio
async def test_delegate_concurrency_capped_at_delegate_concurrency(tmp_path, monkeypatch):
    # Spec §10 concurrency-semaphore case: 4 concurrent _delegate_impl calls
    # must never run more than DELEGATE_CONCURRENCY children at once, and all
    # four must still complete. Uses an asyncio.Event handshake (no sleeps):
    # every child blocks until the 3rd concurrent entry proves the cap was
    # reached, then all release together.
    monkeypatch.setattr(orch, "DELEGATE_CONCURRENCY", 3)
    orch._SEM_BY_LOOP.clear()
    sink = _Sink()
    _parent(sink, tmp_path)
    live = 0
    peak = 0
    at_cap = asyncio.Event()
    completed: list[str] = []

    async def fake_run_subagent(goal, context, expected_output, max_turns, *,
                                parent_ctx, parent_agent, call_id, holder=None):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        if live == orch.DELEGATE_CONCURRENCY:
            at_cap.set()
        await at_cap.wait()      # hold every child until 3 are proven concurrent
        live -= 1
        completed.append(goal)
        return f"done-{goal}"

    with patch("skills.orchestration.run_subagent", side_effect=fake_run_subagent):
        to.CALL_ID_VAR.set("")
        results = await asyncio.gather(*[orch._delegate_impl(f"g{i}", "", "", 5) for i in range(4)])

    assert peak == orch.DELEGATE_CONCURRENCY                      # never exceeded the cap
    assert sorted(completed) == [f"g{i}" for i in range(4)]        # all four ran
    assert sorted(results) == [f"done-g{i}" for i in range(4)]     # all four completed ok


@pytest.mark.asyncio
async def test_child_confirmation_card_reaches_parent_sink_unwrapped(tmp_path):
    # Minor 4: a gated tool inside the child raises its confirmation/access
    # card through the per-module EVENT_QUEUE_VAR it was built around (here
    # skills.filesystem's), NOT through ctx.sink / _WrapSink. run_subagent
    # never touches that var, so the child inherits the parent's sink
    # unchanged and the card must land UNWRAPPED at the parent sink's top
    # level (tasks/driver.py and channels/driver.py key on the top-level
    # `type`) — while the child's own stream events still arrive wrapped as
    # `subagent_event`.
    import skills.filesystem as fsskill
    sink = _Sink()
    _parent(sink, tmp_path)
    gate_token = fsskill.EVENT_QUEUE_VAR.set(sink)
    try:
        inner = [{"type": "message_delta", "content": "hi"}]

        async def _events():
            await fsskill.EVENT_QUEUE_VAR.get().put(
                {"type": "access_request", "call_id": "fs1", "path": "/DATA/x"})
            for e in inner:
                yield e

        m = SimpleNamespace(stream_events=_events, final_output=None, cancel=lambda *a, **k: None)
        with patch("skills.orchestration.Runner.run_streamed", return_value=m), \
             patch("skills.orchestration._convert_event", side_effect=lambda e, n, s: e):
            to.CALL_ID_VAR.set("call_gate2")
            await orch._delegate_impl("g", "", "", 15)
    finally:
        fsskill.EVENT_QUEUE_VAR.reset(gate_token)

    access = next(e for e in sink.events if e["type"] == "access_request")
    assert access == {"type": "access_request", "call_id": "fs1", "path": "/DATA/x"}  # unwrapped
    wrapped = [e["event"] for e in sink.events if e["type"] == "subagent_event"]
    assert wrapped == inner                                        # child's stream events wrapped
