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


class _CollectSink:
    def __init__(self): self.events = []
    async def put(self, e): self.events.append(e)
