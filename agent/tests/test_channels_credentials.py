import httpx
import pytest
from channels import credentials


@pytest.mark.asyncio
async def test_resolve_ok(monkeypatch):
    monkeypatch.setenv("NIMOOS_AI_INTERNAL_URL", "http://ai.test")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={
            "provider_type": "deepseek", "base_url": "https://api.deepseek.com",
            "api_key": "sk-x", "model": "deepseek-chat"})

    out = await credentials.resolve("u1", "cloud:6:deepseek-chat",
                                    transport=httpx.MockTransport(handler))
    assert out["api_key"] == "sk-x" and out["model"] == "deepseek-chat"
    assert "/v1/ai/_internal/agent/provider-credentials" in seen["url"]
    assert "user_id=u1" in seen["url"]


@pytest.mark.asyncio
async def test_resolve_non_200_and_bad_payload(monkeypatch):
    monkeypatch.setenv("NIMOOS_AI_INTERNAL_URL", "http://ai.test")
    t404 = httpx.MockTransport(lambda r: httpx.Response(404, json={}))
    assert await credentials.resolve("u1", "cloud:9:x", transport=t404) is None
    tbad = httpx.MockTransport(lambda r: httpx.Response(200, json={"base_url": ""}))
    assert await credentials.resolve("u1", "m", transport=tbad) is None


@pytest.mark.asyncio
async def test_resolve_reads_url_file(monkeypatch, tmp_path):
    monkeypatch.delenv("NIMOOS_AI_INTERNAL_URL", raising=False)
    (tmp_path / "ai.url").write_text("http://127.0.0.1:40000\n")
    monkeypatch.setenv("NIMOOS_RUNTIME_PATH", str(tmp_path))
    ok = httpx.MockTransport(lambda r: httpx.Response(200, json={
        "provider_type": "ollama", "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "ollama", "model": "qwen3"}))
    out = await credentials.resolve("u1", "qwen3", transport=ok)
    assert out["provider_type"] == "ollama"


@pytest.mark.asyncio
async def test_resolve_no_base_url_available(monkeypatch, tmp_path):
    monkeypatch.delenv("NIMOOS_AI_INTERNAL_URL", raising=False)
    monkeypatch.setenv("NIMOOS_RUNTIME_PATH", str(tmp_path))  # no ai.url file
    assert await credentials.resolve("u1", "m") is None


@pytest.mark.asyncio
async def test_resolve_sends_internal_token(monkeypatch, tmp_path):
    monkeypatch.delenv("NIMOOS_AI_INTERNAL_URL", raising=False)
    (tmp_path / "ai.url").write_text("http://127.0.0.1:40000\n")
    (tmp_path / "ai_internal.token").write_text("known-token-value\n")
    monkeypatch.setenv("NIMOOS_RUNTIME_PATH", str(tmp_path))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Internal-Token") == "known-token-value"
        return httpx.Response(200, json={
            "provider_type": "deepseek", "base_url": "https://api.deepseek.com",
            "api_key": "sk-x", "model": "deepseek-chat"})

    out = await credentials.resolve("u1", "cloud:6:deepseek-chat",
                                    transport=httpx.MockTransport(handler))
    assert out["api_key"] == "sk-x"


@pytest.mark.asyncio
async def test_resolve_non_dict_json_returns_none(monkeypatch):
    monkeypatch.setenv("NIMOOS_AI_INTERNAL_URL", "http://ai.test")
    tnull = httpx.MockTransport(lambda r: httpx.Response(200, json=None))
    assert await credentials.resolve("u1", "m", transport=tnull) is None
    tlist = httpx.MockTransport(lambda r: httpx.Response(200, json=[1]))
    assert await credentials.resolve("u1", "m", transport=tlist) is None
