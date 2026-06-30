# NimoOS-AI/agent/tests/test_mcp_server_e2e.py
import json
from fastapi.testclient import TestClient
import main
import mcp_tokens
from skills.search import search as ssearch

client = TestClient(main.app)
RPC = "/mcp"
ACCEPT = {"Accept": "application/json, text/event-stream",
          "Content-Type": "application/json"}


def _mk_token():
    return mcp_tokens.create(main._conn, "42", "test",
                             now_ms=1)[1]  # plaintext


def _rpc(method, params=None, token=None, _id=1):
    h = dict(ACCEPT)
    if token:
        h["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": _id, "method": method,
            "params": params or {}}
    return client.post(RPC, headers=h, content=json.dumps(body))


def test_no_token_is_401():
    r = _rpc("tools/list")
    assert r.status_code == 401


def test_bad_token_is_401():
    r = _rpc("tools/list", token="nimoos_mcp_nope")
    assert r.status_code == 401


def test_handshake_then_list_tools():
    tok = _mk_token()
    init = _rpc("initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"}}, token=tok)
    assert init.status_code == 200
    listed = _rpc("tools/list", token=tok, _id=2)
    assert listed.status_code == 200
    payload = _extract_json(listed)
    names = {t["name"] for t in payload["result"]["tools"]}
    assert "nimoos_search" in names and "wiki_get_node" in names
    assert "write_file" not in names


def test_call_tool_runs_under_correct_user(monkeypatch):
    captured = {}
    async def fake_search(query, sources=None, filters=None, top_k=5):
        captured["uid"] = ssearch.USER_ID_VAR.get()
        return json.dumps({"hits": []})
    monkeypatch.setattr(ssearch, "_nimoos_search_impl", fake_search)
    tok = _mk_token()
    _rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "p", "version": "0"}}, token=tok)
    r = _rpc("tools/call",
             {"name": "nimoos_search", "arguments": {"query": "x"}},
             token=tok, _id=3)
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
