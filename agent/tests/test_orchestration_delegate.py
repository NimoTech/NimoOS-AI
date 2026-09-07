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
