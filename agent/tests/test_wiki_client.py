"""Tests for agent.wiki_client.WikiClient."""
from __future__ import annotations

import json
import pytest
import httpx

from wiki_client import WikiClient


def _client_with_handler(handler, *, user_id: str = "u1") -> WikiClient:
    """Build a WikiClient whose AsyncClient routes through MockTransport."""
    transport = httpx.MockTransport(handler)
    c = WikiClient("http://wiki.test", user_id=user_id)
    c._http = httpx.AsyncClient(transport=transport, base_url="http://wiki.test")
    return c


@pytest.mark.asyncio
async def test_get_node_returns_json():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/wiki/node"
        assert request.url.params["path"] == "/a"
        assert request.headers.get("X-NimoOS-User-ID") == "u1"
        return httpx.Response(200, json={"path": "/a", "etag": "e1", "ai_label": "L"})

    c = _client_with_handler(handler)
    node = await c.get_node("/a")
    assert node["path"] == "/a"
    assert node["etag"] == "e1"


@pytest.mark.asyncio
async def test_get_node_caches_repeated_calls():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"path": "/a", "etag": "e1"})

    c = _client_with_handler(handler)
    a = await c.get_node("/a")
    b = await c.get_node("/a")
    assert a == b
    assert calls == 1


@pytest.mark.asyncio
async def test_reset_cache_forces_refetch():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"path": "/a", "etag": f"e{calls}"})

    c = _client_with_handler(handler)
    await c.get_node("/a")
    await c.get_node("/a")
    assert calls == 1
    c.reset_cache()
    await c.get_node("/a")
    assert calls == 2


@pytest.mark.asyncio
async def test_invalidate_node_clears_path_and_tree_cache():
    handler_returns = {"node": {"path": "/a", "etag": "e1"}, "tree": [{"path": "/a"}]}
    calls = {"node": 0, "tree": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/wiki/node":
            calls["node"] += 1
            return httpx.Response(200, json=handler_returns["node"])
        if request.url.path == "/v1/wiki/tree":
            calls["tree"] += 1
            return httpx.Response(200, json=handler_returns["tree"])
        return httpx.Response(404)

    c = _client_with_handler(handler)
    await c.get_node("/a")
    await c.list_full_tree()
    c.invalidate_node("/a")
    await c.get_node("/a")
    await c.list_full_tree()
    assert calls["node"] == 2
    assert calls["tree"] == 2


@pytest.mark.asyncio
async def test_list_full_tree_passes_root_id_param():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=[])

    c = _client_with_handler(handler)
    await c.list_full_tree(root_id="r1")
    assert seen["params"].get("root_id") == "r1"


@pytest.mark.asyncio
async def test_recent_changes_passes_since_and_limit():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=[])

    c = _client_with_handler(handler)
    await c.recent_changes(root_id="r1", since_ms=12345, limit=10)
    assert seen["params"]["root_id"] == "r1"
    assert seen["params"]["since_ms"] == "12345"
    assert seen["params"]["limit"] == "10"


@pytest.mark.asyncio
async def test_put_user_notes_sends_if_match_when_present():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["if_match"] = request.headers.get("If-Match")
        seen["body"] = (await request.aread()).decode()
        return httpx.Response(200, json={"etag": "e2"})

    c = _client_with_handler(handler)
    res = await c.put_user_notes("/a", "hi", if_match="e1")
    assert seen["if_match"] == "e1"
    assert seen["body"] == "hi"
    assert res["etag"] == "e2"


@pytest.mark.asyncio
async def test_put_user_notes_no_if_match_when_none():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["if_match"] = request.headers.get("If-Match")
        return httpx.Response(200, json={"etag": "e1"})

    c = _client_with_handler(handler)
    await c.put_user_notes("/a", "hi", if_match=None)
    # No If-Match header → server treats as unconditional write
    assert seen["if_match"] is None


@pytest.mark.asyncio
async def test_put_user_notes_raises_on_409():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, text="etag mismatch")

    c = _client_with_handler(handler)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        await c.put_user_notes("/a", "hi", if_match="e0")
    assert ei.value.response.status_code == 409


@pytest.mark.asyncio
async def test_post_root_sends_payload():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(await request.aread())
        return httpx.Response(200, json={"id": "new-root", "path": "/x", "level": "project"})

    c = _client_with_handler(handler)
    res = await c.post_root("/x", "project")
    assert seen["body"]["path"] == "/x"
    assert seen["body"]["level"] == "project"
    assert res["id"] == "new-root"


def test_try_open_returns_none_when_url_file_missing(tmp_path):
    missing = str(tmp_path / "nope.url")
    assert WikiClient.try_open(url_file=missing) is None


def test_try_open_returns_client_when_url_file_present(tmp_path):
    url_file = tmp_path / "wiki.url"
    url_file.write_text("http://127.0.0.1:39133\n")
    c = WikiClient.try_open(url_file=str(url_file), user_id="u7")
    assert c is not None
    assert c.base == "http://127.0.0.1:39133"
    assert c.user_id == "u7"
