import asyncio

import openai
import notes_extract as ne


class _FakeChoice:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})


class _FakeResp:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    async def create(self, **kwargs):
        return _FakeResp('{"notes":[]}')


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeClient:
    def __init__(self, **kwargs):
        captured["kwargs"] = kwargs
        self.chat = _FakeChat()


captured = {}


def test_default_llm_call_binds_timeout_and_disables_retries(monkeypatch):
    monkeypatch.setattr(openai, "AsyncOpenAI", _FakeClient)
    job = {"provider_url": "http://x", "provider_key": "k", "model_name": "m"}
    out = asyncio.run(ne._default_llm_call(job, "prompt"))
    assert out == '{"notes":[]}'
    assert captured["kwargs"]["timeout"] == ne.LLM_TIMEOUT
    assert captured["kwargs"]["max_retries"] == 0
