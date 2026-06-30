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


class _Sink:
    def __init__(self): self.events = []
    async def put(self, e): self.events.append(e)


@pytest.mark.asyncio
async def test_overhead_passed_and_persisted(runner, monkeypatch):
    seen = {}
    async def fake_compact(conn, **kw):
        seen["overhead"] = kw.get("overhead_tokens")
        return "", kw.get("history")
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
        await runner.run(session_id="s1", user_id="u1", message="hi", sink=_Sink(),
                         provider_key="k", provider_url="http://x", model_name="qwen")

    assert isinstance(seen["overhead"], int) and seen["overhead"] > 0   # system+tools counted
    row = runner._conn.execute(
        "SELECT last_overhead_tokens FROM sessions WHERE id='s1'").fetchone()
    assert row["last_overhead_tokens"] == seen["overhead"]
