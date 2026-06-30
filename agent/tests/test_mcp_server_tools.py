# NimoOS-AI/agent/tests/test_mcp_server_tools.py
import json
import pytest
from mcp_server import tools
from skills.search import search as ssearch
from skills import wiki as swiki


def test_whitelist_is_exactly_six_read_tools():
    names = {d["name"] for d in tools.list_tool_defs()}
    assert names == {
        "nimoos_search", "read_document", "read_file_chunk",
        "wiki_get_node", "wiki_list_full_tree", "wiki_recent_changes",
    }
    # no write/path/photos/vision tools leak in
    for bad in ("write_file", "view_document_page", "search_photos",
                "list_albums", "wiki_append_user_notes"):
        assert bad not in names


def test_read_document_schema_has_no_path_param():
    spec = next(d for d in tools.list_tool_defs() if d["name"] == "read_document")
    props = spec["inputSchema"]["properties"]
    assert "file_id" in props
    assert "path" not in props and "ocr" not in props  # path reads are Plan 2


@pytest.mark.asyncio
async def test_call_dispatches_to_impl(monkeypatch):
    seen = {}
    async def fake_search(query, sources=None, filters=None, top_k=5):
        seen["q"] = query; seen["top_k"] = top_k
        return json.dumps({"ok": True})
    monkeypatch.setattr(ssearch, "_nimoos_search_impl", fake_search)
    out = await tools.call("nimoos_search", {"query": "hi", "top_k": 99})
    assert json.loads(out) == {"ok": True}
    assert seen["q"] == "hi"
    assert seen["top_k"] == 20  # clamped to max 20


@pytest.mark.asyncio
async def test_call_unknown_tool_raises():
    with pytest.raises(KeyError):
        await tools.call("rm_rf", {})


@pytest.mark.asyncio
async def test_setup_user_context_sets_search_and_wiki(monkeypatch):
    tools.setup_user_context("42")
    assert ssearch.USER_ID_VAR.get() == "42"
    client = swiki.WIKI_CLIENT_VAR.get()
    assert client is not None and client.user_id == "42"


@pytest.mark.asyncio
async def test_list_full_tree_truncates(monkeypatch):
    big = [{"path": f"/DATA/{i}", "level": "node"} for i in range(1000)]
    async def fake_impl(root_id=""):
        return json.dumps(big)
    monkeypatch.setattr(swiki, "_wiki_list_full_tree_impl", fake_impl)
    out = json.loads(await tools.call("wiki_list_full_tree", {}))
    assert out["truncated"] is True
    assert len(out["nodes"]) == tools.MAX_TREE_NODES
