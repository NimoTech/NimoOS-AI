"""Tests for agent.skills.wiki read tools + _gate."""
from __future__ import annotations

import json
import pytest

from skills import wiki as wiki_skills
from wiki_client import WikiClient
import httpx


def _make_client(handler) -> WikiClient:
    transport = httpx.MockTransport(handler)
    c = WikiClient("http://wiki.test", user_id="u1")
    c._http = httpx.AsyncClient(transport=transport, base_url="http://wiki.test")
    return c


@pytest.fixture(autouse=True)
def _reset_context():
    """Each test sets ContextVars fresh; pytest fixture resets defaults."""
    wiki_skills.WIKI_CLIENT_VAR.set(None)
    wiki_skills.USER_PATTERNS_VAR.set([])
    yield


@pytest.mark.asyncio
async def test_get_node_returns_error_when_client_none():
    wiki_skills.WIKI_CLIENT_VAR.set(None)
    out = await wiki_skills._wiki_get_node_impl("/a")
    payload = json.loads(out)
    assert payload["error"] == "wiki service unavailable"


@pytest.mark.asyncio
async def test_get_node_returns_node():
    async def h(req):
        return httpx.Response(200, json={"path": "/a", "ai_label": "L"})

    wiki_skills.WIKI_CLIENT_VAR.set(_make_client(h))
    out = await wiki_skills._wiki_get_node_impl("/a")
    payload = json.loads(out)
    assert payload["path"] == "/a"


@pytest.mark.asyncio
async def test_get_node_returns_error_on_404():
    async def h(req):
        return httpx.Response(404)

    wiki_skills.WIKI_CLIENT_VAR.set(_make_client(h))
    out = await wiki_skills._wiki_get_node_impl("/missing")
    payload = json.loads(out)
    assert payload["error"] == "node not found"


@pytest.mark.asyncio
async def test_gate_blocks_blacklisted_path():
    async def h(req):
        pytest.fail("client must not be called")

    wiki_skills.WIKI_CLIENT_VAR.set(_make_client(h))
    wiki_skills.USER_PATTERNS_VAR.set(["secret/**"])
    out = await wiki_skills._wiki_get_node_impl("/secret/file")
    payload = json.loads(out)
    assert payload["error"] == "path is on user's hard blacklist"


@pytest.mark.asyncio
async def test_list_full_tree_passthrough():
    async def h(req):
        return httpx.Response(200, json=[{"path": "/a"}])

    wiki_skills.WIKI_CLIENT_VAR.set(_make_client(h))
    out = await wiki_skills._wiki_list_full_tree_impl("")
    payload = json.loads(out)
    assert payload == [{"path": "/a"}]


@pytest.mark.asyncio
async def test_recent_changes_resolves_root_and_since():
    async def h(req):
        if req.url.path == "/v1/wiki/tree":
            return httpx.Response(200, json=[
                {"path": "/A", "level": "space"},
                {"path": "/A/proj", "level": "project"},
            ])
        if req.url.path == "/v1/wiki/recent-changes":
            params = dict(req.url.params)
            assert params["root_id"] == "/A"  # nearest space ancestor of /A/proj
            assert int(params["since_ms"]) > 0
            assert params["limit"] == "10"
            return httpx.Response(200, json=[{"path": "/A/proj/file", "op": "create"}])
        return httpx.Response(500)

    wiki_skills.WIKI_CLIENT_VAR.set(_make_client(h))
    out = await wiki_skills._wiki_recent_changes_impl("/A/proj", since_days=7, limit=10)
    payload = json.loads(out)
    assert len(payload) == 1
