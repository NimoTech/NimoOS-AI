# NimoOS-AI/agent/tests/test_mcp_server_tools.py
import json
import pytest
from mcp_server import tools
from skills.search import search as ssearch
from skills import wiki as swiki


def test_whitelist_is_exactly_six_read_tools():
    names = {d["name"] for d in tools.list_tool_defs()}
    # core 6 tools are present (photos tools added in Task 3, view_document_page
    # in Task 2 of Plan 2)
    for required in ("nimoos_search", "read_document", "read_file_chunk",
                     "wiki_get_node", "wiki_list_full_tree", "wiki_recent_changes"):
        assert required in names
    # no write tools leak in
    for bad in ("write_file", "wiki_append_user_notes"):
        assert bad not in names


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


@pytest.mark.asyncio
async def test_list_full_tree_error_passthrough(monkeypatch):
    """When the wiki skill returns an error dict, it must not be wrapped in
    a nodes/truncated envelope — it should pass through unchanged."""
    async def fake_impl(root_id=""):
        return json.dumps({"error": "wiki service unavailable"})
    monkeypatch.setattr(swiki, "_wiki_list_full_tree_impl", fake_impl)
    out = json.loads(await tools.call("wiki_list_full_tree", {}))
    assert out == {"error": "wiki service unavailable"}
    # Must NOT be wrapped in a nodes/truncated envelope
    assert "nodes" not in out
    assert "truncated" not in out


from skills import photos as sphotos
import skills.notes as snotes


def test_whitelist_now_has_eight_including_photos():
    names = {d["name"] for d in tools.list_tool_defs()}
    for required in (
        "nimoos_search", "read_document", "read_file_chunk",
        "wiki_get_node", "wiki_list_full_tree", "wiki_recent_changes",
        "search_photos", "list_albums",
    ):
        assert required in names
    for bad in ("create_album", "add_to_album"):
        assert bad not in names


@pytest.mark.asyncio
async def test_call_search_photos_dispatches(monkeypatch):
    seen = {}
    async def fake(query, year=0, limit=20, ocr_text=""):
        seen.update(query=query, limit=limit)
        return json.dumps({"count": 0, "results": []})
    monkeypatch.setattr(sphotos, "_search_photos_impl", fake)
    out = await tools.call("search_photos", {"query": "beach", "limit": 99})
    assert json.loads(out) == {"count": 0, "results": []}
    assert seen["query"] == "beach"
    assert seen["limit"] == 50


@pytest.mark.asyncio
async def test_call_list_albums_dispatches(monkeypatch):
    async def fake():
        return json.dumps({"count": 1, "albums": [{"id": "a", "name": "x"}]})
    monkeypatch.setattr(sphotos, "_list_albums_impl", fake)
    out = json.loads(await tools.call("list_albums", {}))
    assert out["count"] == 1


def test_setup_user_context_sets_photos_uid():
    tools.setup_user_context("42")
    assert sphotos.USER_ID_VAR.get() == "42"


def test_setup_user_context_sets_notes_uid():
    tools.setup_user_context("42")
    assert snotes.USER_ID_VAR.get() == "42"


import asyncio
from mcp_server import fs_gate


def test_whitelist_now_has_eleven_including_notes():
    names = [s["name"] for s in tools.TOOL_SPECS]
    assert len(names) == 11
    assert "list_notes" in names and "read_note" in names


@pytest.mark.asyncio
async def test_call_list_notes_dispatches(monkeypatch):
    import skills.notes as snotes

    async def fake(note_type, status, limit):
        assert (note_type, status, limit) == ("insight", "draft", 5)
        return '{"notes": []}'

    monkeypatch.setattr(snotes, "_list_notes_impl", fake)
    out = await tools.call("list_notes",
                           {"type": "insight", "status": "draft", "limit": 5})
    assert out == '{"notes": []}'


@pytest.mark.asyncio
async def test_call_list_notes_non_integer_limit_raises_clean_error():
    with pytest.raises(tools.McpToolError):
        await tools.call("list_notes", {"limit": "abc"})


@pytest.mark.asyncio
async def test_call_read_note_requires_id():
    with pytest.raises(Exception):
        await tools.call("read_note", {})


def test_read_document_schema_has_file_id_and_path():
    spec = next(d for d in tools.list_tool_defs() if d["name"] == "read_document")
    props = spec["inputSchema"]["properties"]
    assert "file_id" in props and "path" in props and "ocr" in props


@pytest.mark.asyncio
async def test_read_document_rejects_both_file_id_and_path():
    with pytest.raises(tools.McpToolError):
        await tools.call("read_document", {"file_id": "a", "path": "/DATA/x.txt"})


@pytest.mark.asyncio
async def test_read_document_rejects_neither():
    with pytest.raises(tools.McpToolError):
        await tools.call("read_document", {})


@pytest.mark.asyncio
async def test_read_document_path_goes_through_gate(monkeypatch):
    monkeypatch.setattr(fs_gate, "mcp_resolve_read_path",
                        lambda p, root="/DATA", **kw: "/DATA/ok.txt")
    async def fake_extract(path, ocr=False, max_chars=24000, user_id=None):
        assert path == "/DATA/ok.txt"
        return {"text": "hello"}
    monkeypatch.setattr(ssearch._parser_client, "extract", fake_extract)
    out = json.loads(await tools.call("read_document", {"path": "/DATA/x.txt"}))
    assert out["text"] == "hello"


@pytest.mark.asyncio
async def test_read_document_path_denied_raises(monkeypatch):
    def deny(p, root="/DATA", **kw):
        raise fs_gate.McpPathDenied("outside")
    monkeypatch.setattr(fs_gate, "mcp_resolve_read_path", deny)
    with pytest.raises(tools.McpToolError):
        await tools.call("read_document", {"path": "/etc/passwd"})


@pytest.mark.asyncio
async def test_view_document_page_returns_image(monkeypatch):
    monkeypatch.setattr(fs_gate, "mcp_resolve_read_path",
                        lambda p, root="/DATA", **kw: "/DATA/doc.pdf")
    async def fake_render(path, ps, pe, scale=2.0, user_id=None):
        return {"pages": [{"png_b64": "AAA"}]}
    monkeypatch.setattr(ssearch._parser_client, "render_pages", fake_render)
    res = await tools.call("view_document_page", {"path": "/DATA/doc.pdf", "page": 1})
    assert isinstance(res, tools.ImageResult)
    assert res.data_b64 == "AAA" and res.mime == "image/png"


@pytest.mark.asyncio
async def test_view_document_page_denied_raises(monkeypatch):
    def deny(p, root="/DATA", **kw):
        raise fs_gate.McpPathDenied("outside")
    monkeypatch.setattr(fs_gate, "mcp_resolve_read_path", deny)
    with pytest.raises(tools.McpToolError):
        await tools.call("view_document_page", {"path": "/etc/x.pdf"})


@pytest.mark.asyncio
async def test_view_document_page_missing_page_raises(monkeypatch):
    monkeypatch.setattr(fs_gate, "mcp_resolve_read_path",
                        lambda p, root="/DATA", **kw: "/DATA/doc.pdf")
    async def empty_render(path, ps, pe, scale=2.0, user_id=None):
        return {"pages": []}
    monkeypatch.setattr(ssearch._parser_client, "render_pages", empty_render)
    with pytest.raises(tools.McpToolError):
        await tools.call("view_document_page", {"path": "/DATA/doc.pdf", "page": 99})


@pytest.mark.asyncio
async def test_view_document_page_missing_png_raises(monkeypatch):
    monkeypatch.setattr(fs_gate, "mcp_resolve_read_path",
                        lambda p, root="/DATA", **kw: "/DATA/doc.pdf")
    async def render_no_png(path, ps, pe, scale=2.0, user_id=None):
        return {"pages": [{}]}
    monkeypatch.setattr(ssearch._parser_client, "render_pages", render_no_png)
    with pytest.raises(tools.McpToolError):
        await tools.call("view_document_page", {"path": "/DATA/doc.pdf", "page": 1})


@pytest.mark.asyncio
async def test_read_document_gate_receives_caller_identity(monkeypatch):
    """The path gate needs the caller's user_id and the notes root to keep
    other users' notes unreadable; the handler must pass both."""
    seen = {}

    def gate(p, root="/DATA", **kw):
        seen.update(kw)
        return "/DATA/ok.txt"
    monkeypatch.setattr(fs_gate, "mcp_resolve_read_path", gate)

    async def fake_extract(path, ocr=False, max_chars=24000, user_id=None):
        return {"text": "hello"}
    monkeypatch.setattr(ssearch._parser_client, "extract", fake_extract)
    tools.setup_user_context("42", notes_root="/DATA/MyNotes")
    await tools.call("read_document", {"path": "/DATA/x.txt"})
    assert seen == {"user_id": "42", "notes_root": "/DATA/MyNotes"}
