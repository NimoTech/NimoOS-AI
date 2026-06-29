import pytest
from parser_client import ParserClient


class _Resp:
    def __init__(self, data): self._data = data
    def raise_for_status(self): pass
    def json(self): return self._data


@pytest.mark.asyncio
async def test_agent_memory_upsert_posts_payload(monkeypatch):
    pc = ParserClient()
    monkeypatch.setattr(pc, "_resolve_base_url", lambda: "http://p")
    seen = {}
    async def fake_post(url, json=None, **kw):
        seen["url"] = url; seen["json"] = json
        return _Resp({"upserted": len(json["chunks"])})
    monkeypatch.setattr(pc._client, "post", fake_post)
    out = await pc.agent_memory_upsert("u1", "s1",
        [{"chunk_no": 0, "text": "hi", "created_at": 1}])
    assert seen["url"] == "http://p/v1/parser/agent-memory/upsert"
    assert seen["json"]["user_id"] == "u1" and seen["json"]["session_id"] == "s1"
    assert out == {"upserted": 1}


@pytest.mark.asyncio
async def test_agent_memory_query_posts_payload(monkeypatch):
    pc = ParserClient()
    monkeypatch.setattr(pc, "_resolve_base_url", lambda: "http://p")
    seen = {}
    async def fake_post(url, json=None, **kw):
        seen["url"] = url; seen["json"] = json
        return _Resp({"hits": []})
    monkeypatch.setattr(pc._client, "post", fake_post)
    out = await pc.agent_memory_query("u1", "career", top_k=3)
    assert seen["url"] == "http://p/v1/parser/agent-memory/query"
    assert seen["json"] == {"user_id": "u1", "query": "career", "top_k": 3}
    assert out == {"hits": []}
