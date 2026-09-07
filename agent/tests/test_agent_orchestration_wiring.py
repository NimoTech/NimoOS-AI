# tests/test_agent_orchestration_wiring.py
from unittest.mock import MagicMock, patch

import pytest

import agent as agent_module
import run_context as rc
from db import init_db


@pytest.fixture
def runner(tmp_path):
    conn = init_db(str(tmp_path / "w.db"))
    conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at,plan_json) VALUES('s1','u1',0,0,?)",
                 ('[{"id":"a","title":"T","status":"pending","note":""}]',))
    conn.commit()
    return agent_module.AgentRunner(conn)


class _Sink:
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


def _capture():
    seen = {}
    def fake(agent, input_messages, **kwargs):
        seen["agent"] = agent
        seen["ctx"] = rc.current()
        return _fake_stream(input_messages, [{"role": "assistant", "content": "ok"}])
    return seen, fake


@pytest.mark.asyncio
async def test_task_run_gets_orchestration_guidance_and_core_tools(runner):
    seen, fake = _capture()
    sink = _Sink()
    with patch("agent.Runner.run_streamed", side_effect=fake):
        await runner.run(session_id="s1", user_id="u1", message="go", sink=sink,
                         provider_key="k", provider_url="http://x", model_name="qwen", run_context="task")
    names = {getattr(t, "name", "") for t in seen["agent"].tools}
    assert {"update_plan", "delegate"} <= names
    assert agent_module.ORCHESTRATION_GUIDANCE in seen["agent"].instructions
    ctx = seen["ctx"]
    assert ctx.sink is sink and ctx.plan[0]["id"] == "a"


@pytest.mark.asyncio
async def test_interactive_run_has_tools_but_no_guidance(runner):
    seen, fake = _capture()
    with patch("agent.Runner.run_streamed", side_effect=fake):
        await runner.run(session_id="s1", user_id="u1", message="go", sink=_Sink(),
                         provider_key="k", provider_url="http://x", model_name="qwen")
    names = {getattr(t, "name", "") for t in seen["agent"].tools}
    assert {"update_plan", "delegate"} <= names
    assert agent_module.ORCHESTRATION_GUIDANCE not in seen["agent"].instructions


@pytest.mark.asyncio
async def test_channel_run_gets_guidance(runner):
    seen, fake = _capture()
    with patch("agent.Runner.run_streamed", side_effect=fake):
        await runner.run(session_id="s1", user_id="u1", message="go", sink=_Sink(),
                         provider_key="k", provider_url="http://x", model_name="qwen", run_context="channel")
    assert agent_module.ORCHESTRATION_GUIDANCE in seen["agent"].instructions
