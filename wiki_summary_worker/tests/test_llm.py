from __future__ import annotations
import json
import httpx
import pytest

from wiki_summary_worker import llm, sampler
from wiki_summary_worker.config import Config


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://ai.test")


def _evidence():
    return sampler.Evidence(node_path="/x")


def _stub_ai(monkeypatch, handler):
    monkeypatch.setattr(llm, "_make_client", lambda timeout=60: _client(handler))
    monkeypatch.setattr(llm.discovery, "ai_url", lambda: "http://ai.test")
    monkeypatch.setattr(llm.discovery, "resolve_user_id", lambda cfg: "42")


def test_summarize_clean_json(monkeypatch):
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"ai_label": "L", "summary": "S"}'}}]
        })
    _stub_ai(monkeypatch, handler)
    out = llm.summarize(_evidence(), Config())
    assert out == {"ai_label": "L", "summary": "S"}


def test_summarize_markdown_wrapped(monkeypatch):
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '```json\n{"ai_label":"L","summary":"S"}\n```'}}]
        })
    _stub_ai(monkeypatch, handler)
    assert llm.summarize(_evidence(), Config()) == {"ai_label": "L", "summary": "S"}


def test_summarize_explanation_around_json(monkeypatch):
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content":
                'Here is the result: {"ai_label":"L","summary":"S"} hope this helps'}}]
        })
    _stub_ai(monkeypatch, handler)
    assert llm.summarize(_evidence(), Config()) == {"ai_label": "L", "summary": "S"}


def test_summarize_truncates_oversize_fields(monkeypatch):
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({
                "ai_label": "X" * 200,
                "summary": "Y" * 1000,
            })}}]
        })
    _stub_ai(monkeypatch, handler)
    out = llm.summarize(_evidence(), Config())
    assert len(out["ai_label"]) == 80
    assert len(out["summary"]) == 600


def test_summarize_raises_jsonparse_on_garbage(monkeypatch):
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "this is not JSON at all"}}]
        })
    _stub_ai(monkeypatch, handler)
    with pytest.raises(llm.JSONParseError):
        llm.summarize(_evidence(), Config())


def test_summarize_raises_jsonparse_on_missing_fields(monkeypatch):
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"only_label": "L"}'}}]
        })
    _stub_ai(monkeypatch, handler)
    with pytest.raises(llm.JSONParseError):
        llm.summarize(_evidence(), Config())


def test_summarize_raises_jsonparse_on_non_object(monkeypatch):
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '["L", "S"]'}}]
        })
    _stub_ai(monkeypatch, handler)
    with pytest.raises(llm.JSONParseError):
        llm.summarize(_evidence(), Config())


def test_summarize_raises_llmerror_on_5xx(monkeypatch):
    def handler(req):
        return httpx.Response(502, text="upstream down")
    _stub_ai(monkeypatch, handler)
    with pytest.raises(llm.LLMError):
        llm.summarize(_evidence(), Config())


def test_summarize_raises_llmerror_on_network_error(monkeypatch):
    def handler(req):
        raise httpx.ConnectError("connection refused")
    _stub_ai(monkeypatch, handler)
    with pytest.raises(llm.LLMError):
        llm.summarize(_evidence(), Config())


def test_summarize_raises_llmerror_on_bad_shape(monkeypatch):
    def handler(req):
        return httpx.Response(200, json={"oops": "no choices array"})
    _stub_ai(monkeypatch, handler)
    with pytest.raises(llm.LLMError):
        llm.summarize(_evidence(), Config())


def test_summarize_never_leaks_bare_httpx_or_value_error(monkeypatch):
    """Regression: per spec §5.2, only LLMError/JSONParseError may escape."""
    cases = [
        lambda req: (_ for _ in ()).throw(httpx.HTTPError("xxx")),
        lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "{"}}]}),
    ]
    for h in cases:
        _stub_ai(monkeypatch, h)
        with pytest.raises((llm.LLMError, llm.JSONParseError)):
            llm.summarize(_evidence(), Config())


def test_summarize_sends_user_id_header(monkeypatch):
    seen = {}
    def handler(req):
        seen["uid"] = req.headers.get("X-NimoOS-User-ID")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"ai_label":"L","summary":"S"}'}}]
        })
    _stub_ai(monkeypatch, handler)
    llm.summarize(_evidence(), Config())
    assert seen["uid"] == "42", "must use the user-id resolved at call time"
