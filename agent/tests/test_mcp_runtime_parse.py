import json

import pytest

import mcp_client.runtime as rt
from mcp_client.runtime import (_parse_schemas_body, fetch_schemas, parse_runtime,
                                 put_approval, release_token)


def test_parse_runtime_extracts_servers_approvals_and_token():
    payload = json.dumps({
        "servers": [{"id": 1, "name": "gh", "handle": "github", "listed_at": 100,
                     "ttl_sec": 600, "tools": [{"name": "create_issue"}]}],
        "approvals": [{"server_id": 1, "tool_name": "create_issue"},
                      {"server_id": 2, "tool_name": "*"}],
        "write_token": "tok123",
    })
    out = parse_runtime(payload)
    assert out.servers[0]["handle"] == "github"
    assert out.approvals == {"1::create_issue", "2::*"}
    assert out.write_token == "tok123"


def test_parse_runtime_tolerates_missing_new_fields():
    # Must not crash when talking to an older Go build that doesn't send
    # approvals/write_token yet: MCP is an add-on capability and must never
    # prevent a run from starting.
    out = parse_runtime(json.dumps({"servers": [{"id": 1, "name": "gh"}]}))
    assert out.approvals == set() and out.write_token == ""


def test_parse_runtime_malformed_is_failure_not_empty():
    assert parse_runtime("not json") is None


# --- put_approval / fetch_schemas / release_token ---
#
# These three calls all route through _read_ai_base() and send
# X-Agent-MCP-Write-Token. Shared fakes below record what was actually sent
# so the tests can assert on the header/body, not just the return value.


class _FakeResponse:
    def __init__(self, status_code=200, text="{}"):
        self.status_code = status_code
        self.text = text


def _make_fake_client(response=None, raise_exc=None, calls=None):
    """Build a fake httpx.AsyncClient class that records every get/post call
    (method, url, kwargs) into `calls` and returns `response`, or raises
    `raise_exc` if given."""
    if calls is None:
        calls = []

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return False

        async def get(self, url, **kwargs):
            calls.append(("GET", url, kwargs))
            if raise_exc:
                raise raise_exc
            return response

        async def post(self, url, **kwargs):
            calls.append(("POST", url, kwargs))
            if raise_exc:
                raise raise_exc
            return response

    return FakeAsyncClient, calls


@pytest.mark.asyncio
async def test_put_approval_success_sends_write_token_header(monkeypatch):
    monkeypatch.setattr(rt, "_read_ai_base", lambda: "http://127.0.0.1:1")
    FakeAsyncClient, calls = _make_fake_client(response=_FakeResponse(status_code=204))
    monkeypatch.setattr(rt.httpx, "AsyncClient", FakeAsyncClient)

    out = await put_approval("tok123", 1, "create_issue")

    assert out is True
    assert len(calls) == 1
    method, url, kwargs = calls[0]
    assert method == "POST"
    assert url.endswith("/v1/ai/_internal/mcp/approvals")
    assert kwargs["headers"]["X-Agent-MCP-Write-Token"] == "tok123"
    assert kwargs["json"] == {"server_id": 1, "tool_name": "create_issue"}


@pytest.mark.asyncio
async def test_put_approval_non_204_returns_false(monkeypatch):
    monkeypatch.setattr(rt, "_read_ai_base", lambda: "http://127.0.0.1:1")
    FakeAsyncClient, _ = _make_fake_client(response=_FakeResponse(status_code=403))
    monkeypatch.setattr(rt.httpx, "AsyncClient", FakeAsyncClient)

    assert await put_approval("tok123", 1, "create_issue") is False


@pytest.mark.asyncio
async def test_put_approval_network_error_returns_false(monkeypatch):
    monkeypatch.setattr(rt, "_read_ai_base", lambda: "http://127.0.0.1:1")
    FakeAsyncClient, _ = _make_fake_client(raise_exc=RuntimeError("boom"))
    monkeypatch.setattr(rt.httpx, "AsyncClient", FakeAsyncClient)

    assert await put_approval("tok123", 1, "create_issue") is False


@pytest.mark.asyncio
async def test_put_approval_no_base_returns_false_without_network_call(monkeypatch):
    monkeypatch.setattr(rt, "_read_ai_base", lambda: None)
    FakeAsyncClient, calls = _make_fake_client(response=_FakeResponse(status_code=204))
    monkeypatch.setattr(rt.httpx, "AsyncClient", FakeAsyncClient)

    assert await put_approval("tok123", 1, "create_issue") is False
    assert calls == []


@pytest.mark.asyncio
async def test_fetch_schemas_success_sends_write_token_header(monkeypatch):
    monkeypatch.setattr(rt, "_read_ai_base", lambda: "http://127.0.0.1:1")
    body = json.dumps({"listed_at": 100, "schemas": [{"name": "create_issue"}]})
    FakeAsyncClient, calls = _make_fake_client(response=_FakeResponse(status_code=200, text=body))
    monkeypatch.setattr(rt.httpx, "AsyncClient", FakeAsyncClient)

    listed_at, schemas = await fetch_schemas("tok123", 42)

    assert (listed_at, schemas) == (100, [{"name": "create_issue"}])
    method, url, kwargs = calls[0]
    assert method == "GET"
    assert url.endswith("/v1/ai/_internal/mcp/servers/42/schemas")
    assert kwargs["headers"]["X-Agent-MCP-Write-Token"] == "tok123"


@pytest.mark.asyncio
async def test_fetch_schemas_non_200_returns_zero_and_empty(monkeypatch):
    monkeypatch.setattr(rt, "_read_ai_base", lambda: "http://127.0.0.1:1")
    FakeAsyncClient, _ = _make_fake_client(response=_FakeResponse(status_code=401))
    monkeypatch.setattr(rt.httpx, "AsyncClient", FakeAsyncClient)

    assert await fetch_schemas("bad-tok", 42) == (0, [])


@pytest.mark.asyncio
async def test_fetch_schemas_malformed_body_returns_zero_and_empty(monkeypatch):
    monkeypatch.setattr(rt, "_read_ai_base", lambda: "http://127.0.0.1:1")
    FakeAsyncClient, _ = _make_fake_client(response=_FakeResponse(status_code=200, text="not json"))
    monkeypatch.setattr(rt.httpx, "AsyncClient", FakeAsyncClient)

    assert await fetch_schemas("tok123", 42) == (0, [])


def test_parse_schemas_body_missing_listed_at_discards_schemas():
    # listed_at is what Task 13's cache keys on; if it's untrustworthy, the
    # accompanying schemas must be discarded too, or the cache can never
    # invalidate a stale listing.
    out = _parse_schemas_body(json.dumps({"schemas": [{"name": "t"}]}))
    assert out is None, (
        "schemas must be discarded when listed_at is missing, "
        "or the cache can never invalidate"
    )


def test_parse_schemas_body_wrong_typed_listed_at_discards_schemas():
    out = _parse_schemas_body(json.dumps({"listed_at": "x", "schemas": [{"name": "t"}]}))
    assert out is None, (
        "schemas must be discarded when listed_at is wrong-typed, "
        "or the cache can never invalidate"
    )


@pytest.mark.asyncio
async def test_fetch_schemas_missing_listed_at_returns_zero_and_empty(monkeypatch):
    monkeypatch.setattr(rt, "_read_ai_base", lambda: "http://127.0.0.1:1")
    body = json.dumps({"schemas": [{"name": "t"}]})
    FakeAsyncClient, _ = _make_fake_client(response=_FakeResponse(status_code=200, text=body))
    monkeypatch.setattr(rt.httpx, "AsyncClient", FakeAsyncClient)

    out = await fetch_schemas("tok123", 42)
    assert out == (0, []), (
        "schemas must be discarded when listed_at is missing, "
        "or the cache can never invalidate"
    )


@pytest.mark.asyncio
async def test_fetch_schemas_wrong_typed_listed_at_returns_zero_and_empty(monkeypatch):
    monkeypatch.setattr(rt, "_read_ai_base", lambda: "http://127.0.0.1:1")
    body = json.dumps({"listed_at": "x", "schemas": [{"name": "t"}]})
    FakeAsyncClient, _ = _make_fake_client(response=_FakeResponse(status_code=200, text=body))
    monkeypatch.setattr(rt.httpx, "AsyncClient", FakeAsyncClient)

    out = await fetch_schemas("tok123", 42)
    assert out == (0, []), (
        "schemas must be discarded when listed_at is wrong-typed, "
        "or the cache can never invalidate"
    )


@pytest.mark.asyncio
async def test_fetch_schemas_network_error_returns_zero_and_empty(monkeypatch):
    monkeypatch.setattr(rt, "_read_ai_base", lambda: "http://127.0.0.1:1")
    FakeAsyncClient, _ = _make_fake_client(raise_exc=RuntimeError("boom"))
    monkeypatch.setattr(rt.httpx, "AsyncClient", FakeAsyncClient)

    assert await fetch_schemas("tok123", 42) == (0, [])


@pytest.mark.asyncio
async def test_fetch_schemas_no_base_returns_zero_and_empty_without_network_call(monkeypatch):
    monkeypatch.setattr(rt, "_read_ai_base", lambda: None)
    FakeAsyncClient, calls = _make_fake_client(response=_FakeResponse(status_code=200, text="{}"))
    monkeypatch.setattr(rt.httpx, "AsyncClient", FakeAsyncClient)

    assert await fetch_schemas("tok123", 42) == (0, [])
    assert calls == []


@pytest.mark.asyncio
async def test_release_token_sends_write_token_header_and_returns_none(monkeypatch):
    monkeypatch.setattr(rt, "_read_ai_base", lambda: "http://127.0.0.1:1")
    FakeAsyncClient, calls = _make_fake_client(response=_FakeResponse(status_code=204))
    monkeypatch.setattr(rt.httpx, "AsyncClient", FakeAsyncClient)

    out = await release_token("tok123")

    assert out is None
    method, url, kwargs = calls[0]
    assert method == "POST"
    assert url.endswith("/v1/ai/_internal/mcp/token/release")
    assert kwargs["headers"]["X-Agent-MCP-Write-Token"] == "tok123"


@pytest.mark.asyncio
async def test_release_token_network_error_is_swallowed(monkeypatch):
    monkeypatch.setattr(rt, "_read_ai_base", lambda: "http://127.0.0.1:1")
    FakeAsyncClient, _ = _make_fake_client(raise_exc=RuntimeError("boom"))
    monkeypatch.setattr(rt.httpx, "AsyncClient", FakeAsyncClient)

    assert await release_token("tok123") is None


@pytest.mark.asyncio
async def test_release_token_no_base_is_noop_without_network_call(monkeypatch):
    monkeypatch.setattr(rt, "_read_ai_base", lambda: None)
    FakeAsyncClient, calls = _make_fake_client(response=_FakeResponse(status_code=204))
    monkeypatch.setattr(rt.httpx, "AsyncClient", FakeAsyncClient)

    assert await release_token("tok123") is None
    assert calls == []
