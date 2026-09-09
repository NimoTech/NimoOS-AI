import json
from unittest.mock import MagicMock, patch

import pytest

import agent as agent_module
import summarizer as summarizer_module
from ask import pipeline as ask_pipeline
from ask import rewrite as ask_rewrite
from db import init_db

_PLAN_JSON = json.dumps({
    "needs_retrieval": True, "intent": "compare", "answer_shape": "table",
    "queries": [{"q": "265K max turbo", "lang": "en"}, {"q": "265K 睿频", "lang": "zh"}],
})


def test_append_text_handles_str_and_blocks():
    assert agent_module._append_text("q", "EV") == "q\n\nEV"
    assert agent_module._append_text("q", "") == "q"
    blocks = [{"type": "input_text", "text": "q"}, {"type": "input_image", "image_url": "x"}]
    out = agent_module._append_text(blocks, "EV")
    assert out[-1] == {"type": "input_text", "text": "EV"} and len(out) == 3
    assert blocks[-1]["type"] == "input_image"          # input not mutated


class _Sink:
    def __init__(self):
        self.events = []

    async def put(self, e):
        self.events.append(e)


class _FakeCompletions:
    def __init__(self, captured):
        self._captured = captured

    async def create(self, **kw):
        self._captured.append(kw)
        msg = MagicMock()
        msg.content = _PLAN_JSON
        choice = MagicMock()
        choice.message = msg
        resp = MagicMock()
        resp.choices = [choice]
        return resp


class _FakeClient:
    def __init__(self, captured):
        self.chat = MagicMock()
        self.chat.completions = _FakeCompletions(captured)
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.fixture
def search_runner(tmp_path):
    conn = init_db(str(tmp_path / "ask.db"))
    conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at,agent_type) "
                 "VALUES('s1','u1',0,0,'search')")
    conn.commit()
    return agent_module.AgentRunner(conn)


@pytest.mark.asyncio
async def test_run_gives_the_ask_pipeline_a_working_complete(search_runner, monkeypatch):
    """C1 regression: the pipeline used to get complete=None (the plain
    session_summarize_fn has no .complete), so rewrite silently fell back to
    the deterministic keyword plan on every single turn."""
    llm_calls: list[dict] = []
    bg_client = _FakeClient(llm_calls)

    async def fake_resolve(conn, user_id, *, creds_resolver=None):
        return bg_client, "bg-model", {}

    monkeypatch.setattr(summarizer_module, "resolve_background_client", fake_resolve)

    made = {"n": 0}
    real_make = summarizer_module.make_summarizer

    def counting_make(*a, **kw):
        made["n"] += 1
        return real_make(*a, **kw)

    monkeypatch.setattr(summarizer_module, "make_summarizer", counting_make)

    captured: dict = {}

    async def fake_run_guarded(**kwargs):
        captured.update(kwargs)
        # drive the real rewrite stage through the handle the runner passed us
        captured["plan"] = await ask_rewrite.rewrite(
            kwargs["question"], complete=kwargs["complete"])
        return ask_pipeline.AskResult(evidence_block="EVIDENCE-BLOCK")

    monkeypatch.setattr(ask_pipeline, "run_guarded", fake_run_guarded)

    seen = {}

    def fake_run_streamed(agent, input_messages, **kwargs):
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
        await search_runner.run(
            session_id="s1", user_id="u1", message="compare 265K and 245K turbo",
            sink=_Sink(), provider_key="k", provider_url="http://x", model_name="qwen")

    # the pipeline was called, with a usable model handle
    assert captured, "ask pipeline was not invoked for the search profile"
    assert callable(captured["complete"])

    # and that handle really drove the rewrite stage: a model plan, not the fallback
    plan = captured["plan"]
    assert plan.fallback is False
    assert plan.intent == "compare"
    assert [q.q for q in plan.queries] == ["265K max turbo", "265K 睿频"]
    assert llm_calls and llm_calls[0]["model"] == "bg-model"

    # one background client per run: the ask/compaction/mid-run paths share it,
    # and the run's finally closes it exactly once
    assert made["n"] == 1
    assert bg_client.closed is True

    # the evidence block reached the user turn
    assert "EVIDENCE-BLOCK" in json.dumps(seen["input"], ensure_ascii=False)
