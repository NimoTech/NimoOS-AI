# NimoOS-AI/agent/tests/test_mcp_server_e2e.py
import json
import pytest
import mcp.types as mtypes
from fastapi.testclient import TestClient
from mcp_types._types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
import main
import mcp_tokens
from skills.search import search as ssearch

RPC = "/mcp-rpc"
ACCEPT = {"Accept": "application/json, text/event-stream",
          "Content-Type": "application/json"}
# Newest 2026-07-28-era revision this SDK speaks -- used to build the modern
# per-request envelope in `_rpc_modern` below.
_MODERN_PROTOCOL_VERSION = MODERN_PROTOCOL_VERSIONS[-1]


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


def _rpc_modern(method, params=None, token=None, _id=1, *, client):
    """Like `_rpc`, but for methods that only exist in the 2026-07-28 modern
    per-request-envelope era (currently just `server/discover`).

    `_rpc` speaks the legacy `initialize`-handshake era, which the SDK's
    request classifier (`mcp.shared.inbound.classify_inbound_request`) only
    upgrades to the modern era when ALL of these line up:
      * the `MCP-Protocol-Version` header names a modern revision (not one of
        the legacy handshake versions `_rpc` implicitly targets);
      * `params._meta` carries the `io.modelcontextprotocol/protocolVersion`
        and `io.modelcontextprotocol/clientCapabilities` envelope keys;
      * when headers are present, `Mcp-Method` echoes the body's `method`.
    Miss any one of these and the request is classified into the legacy era
    instead, where an unregistered legacy method comes back as a flat
    JSON-RPC -32601 "Method not found" -- which reads exactly like "not wired
    up" but actually just means "asked in the wrong protocol era". Confirmed
    live before writing this helper: a bare `_rpc("server/discover", ...)`
    returns -32601; adding this envelope reaches the real handler and returns
    200 with a `DiscoverResult` body.
    """
    h = dict(ACCEPT)
    if token:
        h["Authorization"] = f"Bearer {token}"
    h["mcp-protocol-version"] = _MODERN_PROTOCOL_VERSION
    h["mcp-method"] = method
    meta = dict((params or {}).get("_meta") or {})
    meta.setdefault(PROTOCOL_VERSION_META_KEY, _MODERN_PROTOCOL_VERSION)
    meta.setdefault(CLIENT_CAPABILITIES_META_KEY, {})
    full_params = dict(params or {})
    full_params["_meta"] = meta
    body = {"jsonrpc": "2.0", "id": _id, "method": method, "params": full_params}
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


def test_tools_call_unknown_tool_is_iserror_not_jsonrpc_error(client):
    """Critical regression guard: an unknown tool name must come back as an
    isError CallToolResult, NOT a JSON-RPC error envelope.

    tools.call() -> tools._BY_NAME[name] raises a bare KeyError for any
    unregistered/typo'd/stale tool name (mcp_server/tools.py). That is not a
    tools.McpToolError, so it only exercises the generic `except Exception`
    fallback added to server._call -- the existing
    test_tools_call_error_is_iserror only covers the McpToolError branch and
    would stay green even if that fallback were deleted.

    Goes over real HTTP (not the raw handler) because the property under test
    is what an external client actually receives on the wire: mcp 2.0's
    runner.py converts an uncaught non-MCPError/ValidationError exception into
    a JSON-RPC `error` object (see mcp/server/runner.py serve_one's
    `except Exception` branch) -- that would show up as `payload["error"]`,
    not `payload["result"]`, if the fallback were missing.
    """
    tok = _mk_token()
    r = _rpc("tools/call",
             {"name": "no_such_tool", "arguments": {}},
             token=tok, _id=99, client=client)
    payload = _extract_json(r)
    assert "error" not in payload, f"got a JSON-RPC error envelope, not isError: {payload}"
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


# ---------------------------------------------------------------------------
# Task 9: server-side pin tests. Zero changes to mcp_server/** or main.py --
# these pin properties that already hold, so nobody regresses them later.
# ---------------------------------------------------------------------------


def test_tool_order_is_deterministic(client):
    """2026-07-28 asks servers to return tools in a stable order so client-side and
    peer prompt caches can hit. Ours already does -- it generates from the fixed
    TOOL_SPECS list. This pins it against someone later switching to a dict walk.

    Two independent, real HTTP requests, same as the brief originally asked for
    (an earlier revision of this test bypassed HTTP entirely -- see git history
    -- to dodge a test_main_fs_endpoints.py bug that has since been fixed;
    see test_server_discover_is_reachable below and the task report for that
    diagnosis. Tool ordering never actually depended on the transport, so once
    the underlying bug was fixed there was no reason left to avoid it here).
    """
    from mcp_server import tools

    tok = _mk_token()
    first = _extract_json(_rpc("tools/list", token=tok, _id=1, client=client))
    second = _extract_json(_rpc("tools/list", token=tok, _id=2, client=client))

    names_first = [t["name"] for t in first["result"]["tools"]]
    names_second = [t["name"] for t in second["result"]["tools"]]

    assert names_first == names_second
    assert names_first == [d["name"] for d in tools.list_tool_defs()]


def test_server_discover_is_reachable(client):
    """server/discover is mandatory in 2026-07-28, and the lowlevel Server ships a
    default handler for it -- assert it is actually wired up and reachable by a
    real client over the real transport, not merely present as an SDK default.

    This has to go through HTTP rather than calling the SDK-registered handler
    directly: the property worth pinning is "an external client can actually
    reach this", and that is exactly the part a direct handler call can't see.
    Concretely, a bare `_rpc("server/discover", token=tok, client=client)`
    verifiably returns JSON-RPC -32601 "Method not found" (confirmed live) --
    `_rpc` only speaks the legacy `initialize`-handshake era, and the SDK's
    request classifier (`mcp.shared.inbound.classify_inbound_request`) requires
    the 2026-07-28 modern per-request envelope (`MCP-Protocol-Version` /
    `Mcp-Method` headers plus `params._meta`'s protocol-version/
    client-capabilities pair) before `server/discover` is even reachable at
    all. `_rpc_modern` (defined near `_rpc` above) builds that envelope; a
    direct call to `_build_lowlevel()`'s handler would skip all of this
    routing and only prove the SDK ships a default -- not that our wiring
    exposes it.

    (Testing this over HTTP only became reliable after fixing a real,
    pre-existing bug: tests/test_main_fs_endpoints.py used to permanently
    repoint `main._conn` via a bare assignment instead of
    `monkeypatch.setattr`, which desynced `_mk_token()`'s writes from the
    connection `mcp_server/server.py::build(conn)` closed over at import time
    -- spurious 401s on every token-authenticated HTTP test in this file when
    run as part of the full suite. Fixed as part of this task; see the task
    report.)
    """
    tok = _mk_token()
    resp = _rpc_modern("server/discover", token=tok, client=client)
    assert resp.status_code == 200
    payload = _extract_json(resp)
    assert "error" not in payload
    result = payload["result"]

    # Self-reports a protocol version it actually supports. The real wire
    # field is `supportedVersions` -- the brief guessed `protocolVersions`/
    # `protocolVersion`, both wrong; confirmed against mcp_types.DiscoverResult
    # and a live call against this server before writing this assertion.
    assert "2026-07-28" in result["supportedVersions"]

    # Capabilities are substantive, not an empty stub -- tools is registered.
    assert "tools" in result["capabilities"]

    # Server identity is stamped into `_meta` per the 2026-07-28 wire format.
    server_info = result["_meta"]["io.modelcontextprotocol/serverInfo"]
    assert server_info["name"] == "nimoos-mcp"
