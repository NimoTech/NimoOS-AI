# NimoOS-AI/agent/tests/test_mcp_server_e2e.py
import json
import pytest
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
    "nimoos_search", "read_document", "read_file_chunk",
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
