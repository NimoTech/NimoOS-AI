# NimoOS-AI/agent/tests/test_mcp_server_e2e.py
import json
import pytest
import mcp.types as mtypes
from fastapi.testclient import TestClient
import main
import mcp_tokens
from skills.search import search as ssearch

RPC = "/mcp-rpc"
ACCEPT = {"Accept": "application/json, text/event-stream",
          "Content-Type": "application/json"}


@pytest.fixture(scope="session")
def client():
    with TestClient(main.app) as c:
        yield c


def _mk_token():
    return mcp_tokens.create(main._conn, "42", "test",
                             now_ms=1)[1]  # plaintext


def _rpc(method, params=None, token=None, _id=1, *, client):
    h = dict(ACCEPT)
    if token:
        h["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": _id, "method": method,
            "params": params or {}}
    return client.post(RPC, headers=h, content=json.dumps(body))


def test_no_token_is_401(client):
    r = _rpc("tools/list", client=client)
    assert r.status_code == 401


def test_bad_token_is_401(client):
    r = _rpc("tools/list", token="nimoos_mcp_nope", client=client)
    assert r.status_code == 401


def test_handshake_then_list_tools(client):
    tok = _mk_token()
    init = _rpc("initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"}}, token=tok, client=client)
    assert init.status_code == 200
    listed = _rpc("tools/list", token=tok, _id=2, client=client)
    assert listed.status_code == 200
    payload = _extract_json(listed)
    names = {t["name"] for t in payload["result"]["tools"]}
    assert "nimoos_search" in names and "wiki_get_node" in names
    assert "write_file" not in names


def test_call_tool_runs_under_correct_user(monkeypatch, client):
    captured = {}
    async def fake_search(query, sources=None, filters=None, top_k=5):
        captured["uid"] = ssearch.USER_ID_VAR.get()
        return json.dumps({"hits": []})
    monkeypatch.setattr(ssearch, "_nimoos_search_impl", fake_search)
    tok = _mk_token()
    _rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "p", "version": "0"}}, token=tok,
         client=client)
    r = _rpc("tools/call",
             {"name": "nimoos_search", "arguments": {"query": "x"}},
             token=tok, _id=3, client=client)
    assert r.status_code == 200
    assert captured["uid"] == "42"  # ContextVar propagated to dispatch


def _extract_json(resp):
    """Streamable-HTTP may answer as application/json or as one SSE 'data:' line."""
    ct = resp.headers.get("content-type", "")
    if "text/event-stream" in ct:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise AssertionError("no SSE data line")
    return resp.json()


# ---------------------------------------------------------------------------
# Regression: bare /mcp-rpc must NOT 307 (trailing-slash redirect bug)
# ---------------------------------------------------------------------------

_EXPECTED_TOOLS = {
    "list_albums", "list_notes", "nimoos_search", "read_document",
    "read_file_chunk", "read_note", "search_photos", "view_document_page",
    "wiki_get_node", "wiki_list_full_tree", "wiki_recent_changes",
}


def test_bare_path_no_redirect_returns_200(client):
    """POST /mcp-rpc (no trailing slash) must return 200, not 307.

    Before the fix, Starlette's redirect_slashes caused /mcp-rpc → 307
    /mcp-rpc/ which is wrong for external clients behind a prefix-stripping
    reverse proxy (e.g. the Go gateway strips /v1/ai before forwarding).
    """
    tok = _mk_token()
    body = {"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {}}
    h = {
        "Authorization": f"Bearer {tok}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    r = client.post("/mcp-rpc", json=body, headers=h, follow_redirects=False)
    assert r.status_code == 200, (
        f"Expected 200 on bare /mcp-rpc, got {r.status_code} "
        f"(Location: {r.headers.get('location', 'n/a')})"
    )
    payload = _extract_json(r)
    names = {t["name"] for t in payload["result"]["tools"]}
    assert names == _EXPECTED_TOOLS, f"Tool names mismatch: {names}"


def test_trailing_slash_still_200(client):
    """POST /mcp-rpc/ (trailing slash) must still return 200."""
    tok = _mk_token()
    body = {"jsonrpc": "2.0", "id": 11, "method": "tools/list", "params": {}}
    h = {
        "Authorization": f"Bearer {tok}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    r = client.post("/mcp-rpc/", json=body, headers=h, follow_redirects=False)
    assert r.status_code == 200, f"Expected 200 on /mcp-rpc/, got {r.status_code}"


def test_bare_path_no_token_is_401_not_307(client):
    """POST /mcp-rpc without auth must return 401, not 307 (redirect would skip auth)."""
    body = {"jsonrpc": "2.0", "id": 12, "method": "tools/list", "params": {}}
    h = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    r = client.post("/mcp-rpc", json=body, headers=h, follow_redirects=False)
    assert r.status_code == 401, (
        f"Expected 401 on bare /mcp-rpc without token, got {r.status_code}"
    )


def test_render_result_maps_types():
    from mcp_server import server as mcpserver
    from mcp_server import tools
    import mcp.types as mt
    text = mcpserver.render_result("hello")
    assert len(text) == 1 and isinstance(text[0], mt.TextContent) and text[0].text == "hello"
    img = mcpserver.render_result(tools.ImageResult("AAA", "image/png"))
    assert len(img) == 1 and isinstance(img[0], mt.ImageContent)
    assert img[0].data == "AAA" and img[0].mime_type == "image/png"


def test_tools_call_error_is_iserror(client):
    # read_document with both file_id and path -> McpToolError -> isError result
    tok = _mk_token()
    r = _rpc("tools/call",
             {"name": "read_document", "arguments": {"file_id": "a", "path": "/DATA/x"}},
             token=tok, _id=42, client=client)
    payload = _extract_json(r)
    assert payload["result"]["isError"] is True


def _get_raw_call_handler():
    """Recover the `_call` handler registered by `_build_lowlevel`.

    mcp 2.0's constructor-injection API (`Server(..., on_call_tool=_call)`)
    stores the handler directly and unwrapped in the server's request-handler
    table — confirmed by reading `mcp/server/lowlevel/server.py`:
    `HandlerEntry(params_type, h)` stores `h` verbatim, no decorator-based
    wrapping survives into 2.0. So `_call` is reachable directly via the
    public `get_request_handler` accessor; no closure unwrapping needed.
    """
    from mcp_server.server import _build_lowlevel

    server = _build_lowlevel()
    entry = server.get_request_handler("tools/call")
    return entry.handler


@pytest.mark.asyncio
async def test_call_mcptoolerror_branch_maps_to_iserror_directly(monkeypatch):
    """Pin the explicit `except tools.McpToolError` branch in `server._call`.

    Invokes the raw `_call` handler directly. Unlike mcp 1.x, the SDK no
    longer wraps registered handlers with a generic `except Exception`
    fallback (`on_call_tool` is stored verbatim — see
    `_get_raw_call_handler`), but this test still pins the branch at the unit
    level: confirmed by temporarily deleting it, this test failed with
    `tools.McpToolError: boom` propagating uncaught, then passed again once
    the branch was restored.
    """
    from mcp_server import tools

    async def fake_call(name, arguments):
        raise tools.McpToolError("boom")

    monkeypatch.setattr(tools, "call", fake_call)

    call = _get_raw_call_handler()
    params = mtypes.CallToolRequestParams(name="whatever", arguments={})
    result = await call(None, params)

    assert isinstance(result, mtypes.CallToolResult)
    assert result.is_error is True
    assert result.content[0].text == "boom"
