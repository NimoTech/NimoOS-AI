# tests/test_agent_context_rescue.py
from unittest.mock import MagicMock, patch

import httpx
import pytest
from openai import BadRequestError

import agent as agent_module
import context_compaction as cc
import model_windows as mw
import run_context as rc
from db import init_db


@pytest.fixture
def runner(tmp_path):
    conn = init_db(str(tmp_path / "r.db"))
    conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) VALUES('s1','u1',0,0)"); conn.commit()
    return agent_module.AgentRunner(conn)


class _Sink:
    def __init__(self): self.events = []
    async def put(self, e): self.events.append(e)


def _bad_request(msg):
    req = httpx.Request("POST", "https://api.x/v1/chat/completions")
    return BadRequestError(msg, response=httpx.Response(400, request=req, json={}), body={"error": {"message": msg}})


def _ok_stream(input_messages, new_items):
    async def _events():
        if False:
            yield None
    m = MagicMock(); m.stream_events = _events
    m.to_input_list.return_value = list(input_messages) + new_items
    m.final_output = "ok"; m.raw_responses = []
    return m


def _failing_stream(input_messages, exc, partial_items):
    async def _events():
        raise exc
        yield None  # noqa
    m = MagicMock(); m.stream_events = _events
    m.to_input_list.return_value = list(input_messages) + partial_items
    m.final_output = None; m.raw_responses = []
    return m


@pytest.mark.asyncio
async def test_context_400_triggers_one_rescue_and_learns_window(runner):
    calls = []
    def fake_run_streamed(agent, input_messages, **kw):
        calls.append((list(input_messages), kw.get("max_turns"), rc.current().window))
        if len(calls) == 1:
            rc.current().extra["llm_calls"] = 3
            rc.current().last_input_tokens = 120_000
            return _failing_stream(input_messages, _bad_request(
                "This model's maximum context length is 131072 tokens. However, you requested 140000 tokens"),
                [{"type": "function_call", "call_id": "c1", "name": "web_fetch", "arguments": "{}"},
                 {"type": "function_call_output", "call_id": "c1", "output": "x" * 9000}])
        return _ok_stream(input_messages, [{"role": "assistant", "content": "done"}])
    sink = _Sink()
    with patch("agent.Runner.run_streamed", side_effect=fake_run_streamed):
        await runner.run(session_id="s1", user_id="u1", message="hi", sink=sink, provider_key="k",
                         provider_url="http://x", model_name="m", max_turns=13)
    assert len(calls) == 2
    assert calls[1][1] == 10                                   # 13 - 3 llm calls used
    # classify() now takes the MIN of the two numbers in the message (the
    # limit, 131072 — not the oversized 140000 request), and the retry window
    # is then forced to a real shrink off that (Major 1): here the retry's
    # own payload is tiny, so the 0.9*estimate term dominates and drives the
    # window well below the classified limit — assert the shrink, not an
    # exact number that depends on the estimator's char-ratio constants.
    assert calls[1][2] < 131_072 and calls[1][2] >= cc.MIN_CONTEXT_WINDOW  # RunCtx.window forced to shrink before the retry
    assert calls[1][0] == calls[0][0] + [{"type": "function_call", "call_id": "c1", "name": "web_fetch", "arguments": "{}"},
                                          {"type": "function_call_output", "call_id": "c1", "output": "x" * 9000}]
    rec = [e for e in sink.events if e["type"] == "context_recovered"]
    assert (len(rec) == 1 and rec[0]["window"] == calls[1][2]
            and rec[0]["before"] > 0 and rec[0]["after"] <= rec[0]["before"])
    assert not any(e["type"] == "error" for e in sink.events)
    assert sink.events[-1]["type"] == "done"
    # The PERSISTED model_windows row is the classified limit (131072, via
    # learn()) — independent of the per-run retry window above, which is a
    # working value only (never written to the store).
    assert mw.get(runner._conn, "cloud:m") == {**mw.get(runner._conn, "cloud:m"), "window": 131_072, "source": "learned"}


@pytest.mark.asyncio
async def test_rescue_window_forced_below_manual_row_and_prev_window(runner):
    # A manual model_windows row (200_000) always "wins" inside
    # model_windows.learn() — it is returned untouched — and a user_settings
    # override (131_072) makes resolve_window() ignore that row entirely for
    # THIS run's starting window. So naively setting RunCtx.window to
    # learn()'s return value would nearly DOUBLE the budget that just 400'd.
    # Major 1's fix forces retry_w <= 0.9 * min(prev window, estimated
    # payload size) regardless of what learn() returns.
    mw.upsert(runner._conn, "cloud:m", 200_000, "manual")
    runner._conn.execute(
        "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES('u1','context_window','131072',0)")
    runner._conn.commit()
    calls = []
    def fake_run_streamed(agent, input_messages, **kw):
        calls.append((list(input_messages), rc.current().window))
        if len(calls) == 1:
            return _failing_stream(input_messages,
                                    _bad_request("maximum context length is 100000 tokens"), [])
        return _ok_stream(input_messages, [{"role": "assistant", "content": "done"}])
    sink = _Sink()
    with patch("agent.Runner.run_streamed", side_effect=fake_run_streamed):
        await runner.run(session_id="s1", user_id="u1", message="hi", sink=sink, provider_key="k",
                         provider_url="http://x", model_name="m")
    assert len(calls) == 2
    assert calls[0][1] == 131_072                                  # user override, not the manual row
    assert calls[1][1] < 100_000 and calls[1][1] >= cc.MIN_CONTEXT_WINDOW
    # the persisted row is untouched: manual rows are never overwritten by learn()
    assert mw.get(runner._conn, "cloud:m") == {**mw.get(runner._conn, "cloud:m"), "window": 200_000, "source": "manual"}


@pytest.mark.asyncio
async def test_rescue_forces_compaction_on_for_the_retry(runner):
    runner._conn.execute(
        "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES('u1','compaction_enabled','0',0)")
    runner._conn.commit()
    enabled_at_call = []
    def fake_run_streamed(agent, input_messages, **kw):
        enabled_at_call.append(rc.current().compaction_enabled)
        if len(enabled_at_call) == 1:
            return _failing_stream(input_messages,
                                    _bad_request("maximum context length is 100000 tokens"), [])
        return _ok_stream(input_messages, [{"role": "assistant", "content": "done"}])
    sink = _Sink()
    with patch("agent.Runner.run_streamed", side_effect=fake_run_streamed):
        await runner.run(session_id="s1", user_id="u1", message="hi", sink=sink, provider_key="k",
                         provider_url="http://x", model_name="m")
    assert enabled_at_call == [False, True]                        # user disabled it; the rescue forces it on


@pytest.mark.asyncio
async def test_second_context_400_falls_through_to_error(runner):
    n = {"i": 0}
    def fake_run_streamed(agent, input_messages, **kw):
        n["i"] += 1
        rc.current().last_input_tokens = 50_000
        return _failing_stream(input_messages, _bad_request("context_length_exceeded"), [])
    sink = _Sink()
    with patch("agent.Runner.run_streamed", side_effect=fake_run_streamed):
        await runner.run(session_id="s1", user_id="u1", message="hi", sink=sink, provider_key="k",
                         provider_url="http://x", model_name="m")
    assert n["i"] == 2
    types = [e["type"] for e in sink.events]
    assert types.count("context_recovered") == 1 and "error" in types and types[-1] == "done"


@pytest.mark.asyncio
async def test_unrelated_400_is_not_rescued(runner):
    n = {"i": 0}
    def fake_run_streamed(agent, input_messages, **kw):
        n["i"] += 1
        return _failing_stream(input_messages, _bad_request("invalid tool_calls: insufficient tool messages"), [])
    sink = _Sink()
    with patch("agent.Runner.run_streamed", side_effect=fake_run_streamed):
        await runner.run(session_id="s1", user_id="u1", message="hi", sink=sink, provider_key="k",
                         provider_url="http://x", model_name="m")
    assert n["i"] == 1 and not any(e["type"] == "context_recovered" for e in sink.events)
    assert any(e["type"] == "error" for e in sink.events)


@pytest.mark.asyncio
async def test_context_400_without_parsable_window_and_no_usage_is_not_rescued(runner):
    n = {"i": 0}
    def fake_run_streamed(agent, input_messages, **kw):
        n["i"] += 1
        return _failing_stream(input_messages, _bad_request("prompt is too long"), [])
    sink = _Sink()
    with patch("agent.Runner.run_streamed", side_effect=fake_run_streamed):
        await runner.run(session_id="s1", user_id="u1", message="hi", sink=sink, provider_key="k",
                         provider_url="http://x", model_name="m")
    assert n["i"] == 1 and not any(e["type"] == "context_recovered" for e in sink.events)


def test_rescue_estimates_shrink_when_over_window():
    items = [{"role": "user", "content": "go"}] + sum(
        [[{"type": "function_call", "call_id": f"c{i}", "name": "t", "arguments": "{}"},
          {"type": "function_call_output", "call_id": f"c{i}", "output": "y" * 4000}] for i in range(20)], [])
    before, after = agent_module._rescue_estimates(items, 8_000)
    assert before > 8_000 and after < before and after <= int(8_000 * 0.9)
