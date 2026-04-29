"""Verify RunRequest accepts thinking + AgentRunner builds correct ModelSettings."""
from unittest.mock import patch

import pytest

from main import RunRequest
from provider_adapters import ThinkingLevel


def test_run_request_accepts_thinking():
    req = RunRequest(
        message="hi",
        model="deepseek-v4-pro",
        thinking={"enabled": True, "level": "high"},
    )
    assert req.thinking is not None
    assert req.thinking.enabled is True
    assert req.thinking.level == ThinkingLevel.HIGH


def test_run_request_thinking_optional():
    req = RunRequest(message="hi", model="x")
    assert req.thinking is None


@pytest.mark.asyncio
async def test_agent_runner_passes_thinking_to_model_settings(monkeypatch):
    """When thinking config is provided, AgentRunner attaches the resulting
    ModelSettings to the Agent (NOT to the OpenAIChatCompletionsModel — that
    constructor doesn't accept model_settings; the Runner reads it off Agent)."""
    from agents import Agent
    from agent import AgentRunner
    from provider_adapters import ThinkingConfig

    captured = {}

    real_agent_init = Agent.__init__

    def spy_agent_init(self, *args, **kwargs):
        captured["model_settings"] = kwargs.get("model_settings")
        return real_agent_init(self, *args, **kwargs)

    monkeypatch.setattr(Agent, "__init__", spy_agent_init)

    # Stub Runner.run_streamed to immediately end without making API calls
    class _FakeStream:
        def stream_events(self):
            async def gen():
                if False:
                    yield None
            return gen()
        def to_input_list(self): return []
        final_output = ""

    monkeypatch.setattr("agent.Runner.run_streamed", lambda *a, **k: _FakeStream())

    # Build minimal sink
    class _Sink:
        async def put(self, ev): pass

    import sqlite3, db as db_module
    conn = db_module.init_db(path=":memory:", snapshots_root="/tmp/snaps_test")
    conn.execute("INSERT INTO sessions(id,user_id,title,created_at,updated_at) "
                 "VALUES('s1','u1','t',1,1)")
    conn.commit()

    runner = AgentRunner(conn)
    await runner.run(
        session_id="s1", user_id="u1", message="hi",
        sink=_Sink(),
        provider_key="k", provider_url="http://x", model_name="deepseek-v4-pro",
        provider_type="deepseek",
        thinking=ThinkingConfig(enabled=True, level=ThinkingLevel.HIGH),
    )

    ms = captured["model_settings"]
    assert ms is not None
    assert ms.extra_body == {"thinking": {"type": "enabled"}}
    assert ms.extra_args["reasoning_effort"] == "max"
