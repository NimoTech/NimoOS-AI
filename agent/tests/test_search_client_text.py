import json

import httpx
import pytest

import search_client as sc


@pytest.mark.asyncio
async def test_search_text_posts_body_and_user_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"hits": [{"file_id": "f1"}], "warnings": []})

    client = sc.SearchClient("http://search.test")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = await client.search_text("265K tdp", user_id="7", top_k=10, rerank=True)
    assert seen["url"] == "http://search.test/v1/search/text"
    assert seen["headers"]["x-nimoos-user-id"] == "7"
    assert seen["body"] == {"query": "265K tdp", "top_k": 10, "rerank": True}
    assert out["hits"][0]["file_id"] == "f1"


@pytest.mark.asyncio
async def test_search_text_raises_with_body_on_error():
    def handler(request):
        return httpx.Response(502, text="parser embed 500: boom")

    client = sc.SearchClient("http://search.test")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError) as ei:
        await client.search_text("q", user_id="1")
    assert "parser embed 500: boom" in str(ei.value)
