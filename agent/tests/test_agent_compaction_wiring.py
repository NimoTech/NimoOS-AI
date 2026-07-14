import json
import uuid
import time
from unittest.mock import patch, MagicMock

import pytest

import agent as agent_module
import context_compaction as cc
from db import init_db


@pytest.fixture
def runner(tmp_path):
    conn = init_db(str(tmp_path / "m.db"))
    conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) "
                 "VALUES('s1','u1',0,0)"); conn.commit()
    return agent_module.AgentRunner(conn)


@pytest.mark.asyncio
async def test_make_summarize_fn_calls_client():
    captured = {}

    class FakeMsg:
        content = "  ROLLED  "

    class FakeChoice:
        message = FakeMsg()

    class FakeResp:
        choices = [FakeChoice()]

    class FakeCompletions:
        async def create(self, **kw):
            captured.update(kw)
            return FakeResp()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    fn = agent_module._make_summarize_fn(FakeClient(), "qwen")
    out = await fn("INSTR", "PRIOR", "FOLD")
    assert out == "ROLLED"
    assert captured["model"] == "qwen"
    # instruction as system, prior+fold as user content
    assert captured["messages"][0]["role"] == "system"
    assert "FOLD" in captured["messages"][1]["content"]


@pytest.mark.asyncio
async def test_run_injects_summary_and_uses_compacted_history(runner, monkeypatch):
    # force compaction to return a known summary block + a tiny send_history
    async def fake_compact(conn, **kw):
        return ("[对话历史摘要(较早内容已压缩)]\nSUM", [{"role": "user", "content": "kept"}])
    monkeypatch.setattr(cc, "compact_for_run", fake_compact)

    seen = {}

    def fake_run_streamed(agent, input_messages, **kwargs):
        seen["instructions"] = agent.instructions
        seen["input"] = input_messages
        m = MagicMock()
        async def empty():
            return
            yield
        m.stream_events = empty
        m.to_input_list.return_value = []
        m.final_output = ""
        return m

    with patch("agent.Runner.run_streamed", side_effect=fake_run_streamed):
        sink = _CollectSink()
        await runner.run(
            session_id="s1", user_id="u1", message="新问题", sink=sink,
            provider_key="k", provider_url="http://x", model_name="qwen")

    assert "SUM" in seen["instructions"]
    # compacted send_history ("kept") precedes the new user message
    assert seen["input"][0]["content"] == "kept"
    assert seen["input"][-1]["role"] == "user"


@pytest.mark.asyncio
async def test_continue_run_passes_empty_current_text(runner, monkeypatch):
    # continue_run has no new user message; it must not be double-counted in
    # the compaction token estimate → current_text == "".
    seen = {}

    async def fake_compact(conn, **kw):
        seen["current_text"] = kw.get("current_text")
        return ("", kw.get("history"))
    monkeypatch.setattr(cc, "compact_for_run", fake_compact)

    def fake_run_streamed(agent, input_messages, **kwargs):
        m = MagicMock()
        async def empty():
            return
            yield
        m.stream_events = empty
        m.to_input_list.return_value = []
        m.final_output = ""
        return m

    with patch("agent.Runner.run_streamed", side_effect=fake_run_streamed):
        await runner.run(
            session_id="s1", user_id="u1", message="", sink=_CollectSink(),
            provider_key="k", provider_url="http://x", model_name="qwen",
            continue_run=True)

    assert seen["current_text"] == ""


def _seed_history(runner, session_id, history):
    runner._save_history(session_id, history)


_OLD_HISTORY = [
    {"role": "user", "content": "turn1"},
    {"role": "assistant", "content": "answer1"},
    {"role": "user", "content": "turn2"},
    {"role": "assistant", "content": "answer2"},
]


def _fake_stream(input_messages, new_items):
    m = MagicMock()

    async def empty():
        return
        yield
    m.stream_events = empty
    m.to_input_list.return_value = list(input_messages) + new_items
    m.final_output = ""
    return m


@pytest.mark.asyncio
async def test_compaction_truncation_does_not_lose_saved_history(runner, monkeypatch):
    # Compaction may trim what is SENT to the model, but the persisted history
    # (the /messages data source) must keep the dropped prefix. Regression:
    # to_input_list() of a truncated input used to be saved as the new full
    # snapshot, permanently erasing older turns from the UI.
    _seed_history(runner, "s1", _OLD_HISTORY)

    async def fake_compact(conn, **kw):
        return ("", kw["history"][2:])   # model only sees the last turn
    monkeypatch.setattr(cc, "compact_for_run", fake_compact)

    def fake_run_streamed(agent, input_messages, **kwargs):
        return _fake_stream(
            input_messages, [{"role": "assistant", "content": "new answer"}])

    with patch("agent.Runner.run_streamed", side_effect=fake_run_streamed):
        await runner.run(
            session_id="s1", user_id="u1", message="turn3", sink=_CollectSink(),
            provider_key="k", provider_url="http://x", model_name="qwen")

    saved = runner._load_history("s1")
    contents = [m.get("content") for m in saved]
    assert contents[:2] == ["turn1", "answer1"]          # dropped prefix kept
    assert "turn2" in contents and "answer2" in contents
    assert contents[-2:] == ["turn3", "new answer"]      # new turn appended


@pytest.mark.asyncio
async def test_compaction_truncation_error_path_keeps_prefix(runner, monkeypatch):
    # The error path also persists the partial turn — it must prepend the
    # dropped prefix too.
    _seed_history(runner, "s1", _OLD_HISTORY)

    async def fake_compact(conn, **kw):
        return ("", kw["history"][2:])
    monkeypatch.setattr(cc, "compact_for_run", fake_compact)

    def fake_run_streamed(agent, input_messages, **kwargs):
        m = MagicMock()

        async def boom():
            raise RuntimeError("provider exploded")
            yield
        m.stream_events = boom
        m.to_input_list.return_value = list(input_messages)
        m.final_output = ""
        return m

    with patch("agent.Runner.run_streamed", side_effect=fake_run_streamed):
        sink = _CollectSink()
        await runner.run(
            session_id="s1", user_id="u1", message="turn3", sink=sink,
            provider_key="k", provider_url="http://x", model_name="qwen")

    assert any(e.get("type") == "error" for e in sink.events)
    saved = runner._load_history("s1")
    contents = [m.get("content") for m in saved]
    assert contents[:2] == ["turn1", "answer1"]


class _CollectSink:
    def __init__(self): self.events = []
    async def put(self, e): self.events.append(e)
