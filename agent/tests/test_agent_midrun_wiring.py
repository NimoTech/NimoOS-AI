import inspect
from unittest.mock import MagicMock, patch

import pytest

import agent as agent_module
import compaction_filter as cf
import context_compaction as cc
import phoenix_tracing
import run_context as rc
from db import init_db


@pytest.fixture
def runner(tmp_path):
    conn = init_db(str(tmp_path / "w.db"))
    conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) VALUES('s1','u1',0,0)"); conn.commit()
    return agent_module.AgentRunner(conn)


class _CollectSink:
    def __init__(self): self.events = []
    async def put(self, e): self.events.append(e)


def _fake_stream(input_messages, new_items):
    async def _events():
        if False:
            yield None
    m = MagicMock()
    m.stream_events = _events
    m.to_input_list.return_value = list(input_messages) + new_items
    m.final_output = ""
    m.raw_responses = []
    return m


def test_run_config_factory_accepts_filter():
    cfg = phoenix_tracing.build_trace_run_config(False, "s", "u", "m", "chat",
                                                 call_model_input_filter=cf.compaction_filter)
    assert cfg.call_model_input_filter is cf.compaction_filter
    cfg2 = phoenix_tracing.build_trace_run_config(False, "s", "u", "m", "chat")
    assert cfg2.call_model_input_filter is None


@pytest.mark.asyncio
async def test_run_passes_hooks_filter_and_sets_ctx(runner, monkeypatch):
    seen = {}
    def fake_run_streamed(agent, input_messages, **kwargs):
        seen["hooks"] = kwargs.get("hooks")
        seen["filter"] = getattr(kwargs.get("run_config"), "call_model_input_filter", None)
        seen["ctx"] = rc.current()
        return _fake_stream(input_messages, [{"role": "assistant", "content": "ok"}])
    with patch("agent.Runner.run_streamed", side_effect=fake_run_streamed):
        await runner.run(session_id="s1", user_id="u1", message="hi", sink=_CollectSink(),
                         provider_key="k", provider_url="http://x", model_name="qwen")
    assert isinstance(seen["hooks"], cf.ContextHooks)
    assert seen["filter"] is cf.compaction_filter
    ctx = seen["ctx"]
    assert ctx is not None and ctx.session_id == "s1" and ctx.window == cc.CLOUD_CONTEXT_WINDOW
    assert ctx.summarize_fn is not None and ctx.overhead_tokens > 0
    assert rc.current() is None                          # cleared after the run


@pytest.mark.asyncio
async def test_run_end_persists_l2_state(runner, monkeypatch):
    def fake_run_streamed(agent, input_messages, **kwargs):
        ctx = rc.current()
        ctx.summary, ctx.fold_idx, ctx.l2_count = "ROLLED", 3, 1      # simulate a mid-run fold
        return _fake_stream(input_messages, [{"role": "assistant", "content": "ok"}])
    with patch("agent.Runner.run_streamed", side_effect=fake_run_streamed):
        await runner.run(session_id="s1", user_id="u1", message="hi", sink=_CollectSink(),
                         provider_key="k", provider_url="http://x", model_name="qwen")
    row = runner._conn.execute("SELECT rolling_summary, folded_upto FROM sessions WHERE id='s1'").fetchone()
    assert row["rolling_summary"] == "ROLLED" and row["folded_upto"] == 3


@pytest.mark.asyncio
async def test_history_persisted_is_original_not_filtered(runner):
    """R1 regression: the filter rewrites only the outgoing copy."""
    big = "x" * 5000
    hist = [{"role": "user", "content": "t1"}] + sum(
        [[{"type": "function_call", "call_id": f"c{i}", "name": "web_fetch", "arguments": "{}"},
          {"type": "function_call_output", "call_id": f"c{i}", "output": big}] for i in range(12)], [])
    runner._save_history("s1", hist)
    def fake_run_streamed(agent, input_messages, **kwargs):
        return _fake_stream(input_messages, [{"role": "assistant", "content": "ok"}])
    with patch("agent.Runner.run_streamed", side_effect=fake_run_streamed):
        await runner.run(session_id="s1", user_id="u1", message="t2", sink=_CollectSink(),
                         provider_key="k", provider_url="http://x", model_name="qwen")
    saved = runner._load_history("s1")
    outs = [m for m in saved if m.get("type") == "function_call_output"]
    assert len(outs) == 12 and all(m["output"] == big for m in outs)


def test_run_streamed_call_site_has_hooks():
    src = inspect.getsource(agent_module.AgentRunner.run)
    assert "hooks=" in src and "compaction_filter" in src
