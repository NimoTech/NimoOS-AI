import asyncio

import openai
import notes_distill


class _FakeChoice:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})


class _FakeResp:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    async def create(self, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeResp("summary")


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeClient:
    def __init__(self, **kwargs):
        self.chat = _FakeChat()

    async def close(self):
        pass


captured = {}


def _creds(provider_type):
    return {"provider_type": provider_type, "base_url": "http://x/v1",
            "api_key": "k", "model": "m"}


def test_ollama_creds_disable_thinking_via_extra_body(monkeypatch):
    """I4: qwen-family local models served through Ollama burn 2500+
    reasoning tokens and blow LLM_TIMEOUT unless thinking is explicitly
    switched off — reuse provider_adapters' house think-switch constants."""
    monkeypatch.setattr(openai, "AsyncOpenAI", _FakeClient)
    captured.clear()
    out = asyncio.run(notes_distill._default_llm_call(_creds("ollama"), "prompt"))
    assert out == "summary"
    assert captured["kwargs"]["extra_body"] == {
        "think": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_non_ollama_creds_get_no_think_switch(monkeypatch):
    """Cloud providers (deepseek/openai/anthropic/other) already have their
    own thinking controls upstream of this worker (or none at all) — the
    ollama-specific extra_body must not leak onto them."""
    monkeypatch.setattr(openai, "AsyncOpenAI", _FakeClient)
    captured.clear()
    out = asyncio.run(notes_distill._default_llm_call(_creds("deepseek"), "prompt"))
    assert out == "summary"
    assert "extra_body" not in captured["kwargs"]
