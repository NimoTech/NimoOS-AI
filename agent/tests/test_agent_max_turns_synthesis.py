"""Search profile: exhausting max_turns ends in one tool-less synthesis call,
not an empty transcript (2026-09-09 Intel2408 eval, Q29/Q39)."""
from unittest.mock import MagicMock, patch

import pytest
from agents.exceptions import MaxTurnsExceeded

import agent as agent_module
import summarizer as summarizer_module
from ask import pipeline as ask_pipeline
from db import init_db
from profiles import PROFILES


class _Sink:
    def __init__(self):
        self.events = []

    async def put(self, e):
        self.events.append(e)


class _FakeClient:
    def __init__(self):
        self.chat = MagicMock()

    async def close(self):
        pass


_TRANSCRIPT = [
    {"role": "user", "content": "list every Meteor Lake H model"},
    {"type": "function_call", "call_id": "c1", "name": "nimoos_search", "arguments": "{}"},
    {"type": "function_call_output", "call_id": "c1", "output": "hits..."},
]


def _exhausted_stream():
    m = MagicMock()

    async def boom():
        raise MaxTurnsExceeded("Max turns (5) exceeded")
        yield  # noqa: unreachable — makes this an async generator
    m.stream_events = boom
    m.to_input_list.return_value = list(_TRANSCRIPT)
    m.final_output = None
    return m


def _answer_stream(text="Meteor Lake H: 155H, 165H, 185H [1][3]"):
    m = MagicMock()

    async def empty():
        return
        yield
    m.stream_events = empty
    m.to_input_list.return_value = list(_TRANSCRIPT) + [
        {"role": "assistant", "content": text}]
    m.final_output = text
    m.raw_responses = []
    return m


@pytest.fixture
def runner(tmp_path, monkeypatch):
    conn = init_db(str(tmp_path / "ask.db"))
    conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at,agent_type) "
                 "VALUES('s1','u1',0,0,'search')")
    conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) VALUES('g1','u1',0,0)")
    conn.commit()

    async def fake_resolve(conn, user_id, *, creds_resolver=None):
        return _FakeClient(), "bg-model", {}
    monkeypatch.setattr(summarizer_module, "resolve_background_client", fake_resolve)

    async def fake_run_guarded(**kwargs):
        return ask_pipeline.AskResult(evidence_block="")
    monkeypatch.setattr(ask_pipeline, "run_guarded", fake_run_guarded)
    return agent_module.AgentRunner(conn)


def test_search_profile_opts_in_and_general_does_not():
    assert PROFILES["search"].synthesize_on_max_turns is True
    assert PROFILES["general"].synthesize_on_max_turns is False


@pytest.mark.asyncio
async def test_search_profile_synthesizes_with_tools_off_after_max_turns(runner):
    calls = []

    def fake_run_streamed(agent, input_messages, **kwargs):
        calls.append({"input": input_messages, "kwargs": kwargs,
                      "tool_choice": agent.model_settings.tool_choice,
                      "instructions": str(agent.instructions or "")})
        return _exhausted_stream() if len(calls) == 1 else _answer_stream()

    sink = _Sink()
    with patch("agent.Runner.run_streamed", side_effect=fake_run_streamed):
        await runner.run(session_id="s1", user_id="u1", message="list every Meteor Lake H model",
                         sink=sink, provider_key="k", provider_url="http://x", model_name="qwen")

    assert len(calls) == 2
    first, second = calls
    assert first["kwargs"]["max_turns"] == 5                      # the profile cap
    assert first["tool_choice"] in (None, "auto")
    # the synthesis call: whole transcript in, one turn, no tools, explicit notice
    assert second["input"] == _TRANSCRIPT
    assert second["kwargs"]["max_turns"] == 1
    assert second["tool_choice"] == "none"
    assert agent_module.MAX_TURNS_SYNTHESIS_NOTICE in second["instructions"]

    types = [e["type"] for e in sink.events]
    assert "max_turns_exceeded" not in types
    i_synth = types.index("max_turns_synthesized")
    assert sink.events[i_synth]["max_turns"] == 5
    i_msg = next(i for i, e in enumerate(sink.events) if e["type"] == "message")
    assert i_synth < i_msg and "155H" in sink.events[i_msg]["content"]
    # the answer, not the exhausted transcript, is what got persisted
    saved = runner._load_history("s1")
    assert saved[-1] == {"role": "assistant", "content": "Meteor Lake H: 155H, 165H, 185H [1][3]"}


@pytest.mark.asyncio
async def test_general_profile_keeps_the_resumable_pause(runner):
    calls = []

    def fake_run_streamed(agent, input_messages, **kwargs):
        calls.append(kwargs)
        return _exhausted_stream()

    sink = _Sink()
    with patch("agent.Runner.run_streamed", side_effect=fake_run_streamed):
        await runner.run(session_id="g1", user_id="u1", message="hi", sink=sink,
                         provider_key="k", provider_url="http://x", model_name="qwen", max_turns=5)
    assert len(calls) == 1
    types = [e["type"] for e in sink.events]
    assert "max_turns_synthesized" not in types
    assert {"type": "max_turns_exceeded", "max_turns": 5} in sink.events


@pytest.mark.asyncio
async def test_failed_synthesis_falls_back_to_the_pause_event(runner):
    calls = []

    def fake_run_streamed(agent, input_messages, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _exhausted_stream()
        raise RuntimeError("provider rejected tool_choice=none")

    sink = _Sink()
    with patch("agent.Runner.run_streamed", side_effect=fake_run_streamed):
        await runner.run(session_id="s1", user_id="u1", message="q", sink=sink,
                         provider_key="k", provider_url="http://x", model_name="qwen")
    assert len(calls) == 2
    assert {"type": "max_turns_exceeded", "max_turns": 5} in sink.events
    assert not any(e["type"] == "message" for e in sink.events)
    # the exhausted run's own transcript is what survives
    assert runner._load_history("s1") == _TRANSCRIPT


@pytest.mark.asyncio
async def test_empty_synthesis_falls_back_to_the_pause_event(runner):
    calls = []

    def fake_run_streamed(agent, input_messages, **kwargs):
        calls.append(kwargs)
        return _exhausted_stream() if len(calls) == 1 else _answer_stream(text="")

    sink = _Sink()
    with patch("agent.Runner.run_streamed", side_effect=fake_run_streamed):
        await runner.run(session_id="s1", user_id="u1", message="q", sink=sink,
                         provider_key="k", provider_url="http://x", model_name="qwen")
    assert {"type": "max_turns_exceeded", "max_turns": 5} in sink.events
