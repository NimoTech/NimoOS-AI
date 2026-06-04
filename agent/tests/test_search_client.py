import httpx
import pytest
from unittest.mock import AsyncMock, patch

from search_client import SearchClient


@pytest.mark.asyncio
async def test_invoke_tool_posts_correct_shape():
    async def fake_post(url, json=None, headers=None):
        class R:
            status_code = 200
            def json(self): return {"hits": []}
            def raise_for_status(self): pass
        assert url == "http://127.0.0.1/v1/search/agent/tool"
        assert json == {"name": "nimoos_search", "arguments": {"query": "hi"}}
        assert headers == {"X-NimoOS-User-ID": "u1"}
        return R()

    client = SearchClient()
    with patch.object(client._client, "post", side_effect=fake_post):
        out = await client.invoke_tool("nimoos_search", {"query": "hi"}, user_id="u1")
    assert out == {"hits": []}
    await client.aclose()


@pytest.mark.asyncio
async def test_invoke_tool_includes_response_body_on_error():
    request = httpx.Request("POST", "http://127.0.0.1/v1/search/agent/tool")
    response = httpx.Response(
        400, request=request,
        json={"message": "parser embed 500: Internal Server Error"})

    async def fake_post(url, json=None, headers=None):
        return response

    client = SearchClient()
    with patch.object(client._client, "post", side_effect=fake_post):
        with pytest.raises(httpx.HTTPStatusError) as ei:
            await client.invoke_tool("nimoos_search", {"query": "hi"}, user_id="u1")
    # The raised error carries the response body, not just the bare status line.
    assert "parser embed 500: Internal Server Error" in str(ei.value)
    await client.aclose()
