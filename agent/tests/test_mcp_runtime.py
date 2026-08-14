import asyncio
import json
import time

import pytest

import mcp_client.runtime as rt
from mcp_client.runtime import (AI_URL_PATH, ConfigUnavailable, RuntimePayload,
                                fetch_runtime, parse_servers)


def test_parse_servers_valid():
    payload = json.dumps({"servers": [
        {"id": 1, "name": "github", "transport": "http", "url": "https://x",
         "command": "", "args": [], "env": {}, "headers": {"Authorization": "Bearer x"}}
    ]})
    servers = parse_servers(payload)
    assert len(servers) == 1
    assert servers[0]["name"] == "github"
    assert servers[0]["headers"]["Authorization"] == "Bearer x"


def test_parse_servers_config_error_field_passes_through():
    # Go marks undecryptable configs (Task 9); the field must survive parsing.
    payload = json.dumps({"servers": [
        {"id": 2, "name": "broken", "transport": "http", "url": "https://x",
         "config_error": "credential decryption failed"}]})
    assert parse_servers(payload)[0]["config_error"] == "credential decryption failed"


def test_parse_servers_empty_list_means_no_servers():
    assert parse_servers(json.dumps({"servers": []})) == []


def test_parse_servers_malformed_returns_none():
    # Malformed is a FAILURE, not "no servers" — defect-1 silent point 2.
    assert parse_servers("not json") is None
    assert parse_servers(json.dumps({"unexpected": 1})) is None
    assert parse_servers(json.dumps({"servers": "nope"})) is None


def test_ai_url_path_constant():
    assert AI_URL_PATH.endswith("/var/run/nimoos/ai.url")


# --- fetch_runtime: the sole production entry point for run start (Task 16
# review C2/finding 2) — parses the FULL Runtime response (server list +
# approvals + write_token) via parse_runtime. fetch_mcp_servers (the older,
# parse_servers-only predecessor) was deleted once this replaced its one
# production call site, so these tests are the only coverage of this
# fetch-and-degrade logic. ---

class _FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class _FakeAsyncClient:
    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def get(self, *args, **kwargs):
        if self._raises is not None:
            raise self._raises
        return self._response


def _install_fake_client(monkeypatch, *, response=None, raises=None):
    monkeypatch.setattr(rt, "_read_ai_base", lambda: "http://127.0.0.1:1")
    monkeypatch.setattr(rt.httpx, "AsyncClient",
                        lambda **kw: _FakeAsyncClient(response=response, raises=raises))


@pytest.mark.asyncio
async def test_fetch_runtime_without_ticket_is_config_unavailable():
    out = await fetch_runtime("")
    assert isinstance(out, ConfigUnavailable) and out.reason


@pytest.mark.asyncio
async def test_fetch_runtime_without_ai_url_is_config_unavailable(monkeypatch):
    monkeypatch.setattr(rt, "_read_ai_base", lambda: None)
    out = await fetch_runtime("tok")
    assert isinstance(out, ConfigUnavailable) and "ai.url" in out.reason


@pytest.mark.asyncio
async def test_fetch_runtime_non_200_status_is_config_unavailable(monkeypatch):
    _install_fake_client(monkeypatch, response=_FakeResponse(status_code=500))
    out = await fetch_runtime("tok")
    assert isinstance(out, ConfigUnavailable) and "500" in out.reason


@pytest.mark.asyncio
async def test_fetch_runtime_request_exception_is_config_unavailable(monkeypatch):
    _install_fake_client(monkeypatch, raises=RuntimeError("boom"))
    out = await fetch_runtime("tok")
    assert isinstance(out, ConfigUnavailable) and "boom" in out.reason


@pytest.mark.asyncio
async def test_fetch_runtime_malformed_body_is_config_unavailable(monkeypatch):
    _install_fake_client(monkeypatch, response=_FakeResponse(status_code=200, text="not json"))
    out = await fetch_runtime("tok")
    assert isinstance(out, ConfigUnavailable) and "malformed" in out.reason


@pytest.mark.asyncio
async def test_fetch_runtime_old_build_missing_fields_degrades_to_empty_payload(monkeypatch):
    """An older Go build's response has "servers" but not yet "approvals"/
    "write_token" (parse_runtime's own tolerated shape, Task 12). This must
    still be a usable RuntimePayload with empty defaults, never
    ConfigUnavailable — MCP must not fail to start just because those two
    fields are missing."""
    body = json.dumps({"servers": [{"id": 1, "name": "gh"}]})
    _install_fake_client(monkeypatch, response=_FakeResponse(status_code=200, text=body))
    out = await fetch_runtime("tok")
    assert isinstance(out, RuntimePayload)
    assert out.servers == [{"id": 1, "name": "gh"}]
    assert out.approvals == set()
    assert out.write_token == ""


@pytest.mark.asyncio
async def test_fetch_runtime_success_carries_approvals_and_token(monkeypatch):
    body = json.dumps({
        "servers": [{"id": 1, "name": "gh", "handle": "github"}],
        "approvals": [{"server_id": 1, "tool_name": "create_issue"}],
        "write_token": "tok123",
    })
    _install_fake_client(monkeypatch, response=_FakeResponse(status_code=200, text=body))
    out = await fetch_runtime("tok")
    assert isinstance(out, RuntimePayload)
    assert out.servers[0]["handle"] == "github"
    assert out.approvals == {"1::create_issue"}
    assert out.write_token == "tok123"


# --- fetch_schemas's run-start budget (Task 17 review finding 3) ---
#
# Task 17 deleted _metas_for_server's own connect+list budget
# (MCP_COLD_TOTAL_TIMEOUT, enforced with an explicit asyncio.wait_for around
# a direct connect) because _metas_for_server no longer connects to a
# third-party server at all — it asks Go for schemas over loopback via
# fetch_schemas instead. That moved the ONLY bound left between a hung/slow
# probe and a run start that never returns into fetch_schemas's own
# httpx.AsyncClient(timeout=FETCH_TIMEOUT). Nothing was pinning that bound;
# this test exercises it against a REAL hanging endpoint (a socket that
# accepts the connection and then never writes a byte), not a mock, so a
# regression that drops the `timeout=` kwarg would actually be caught.
@pytest.mark.asyncio
async def test_fetch_schemas_gives_up_promptly_on_a_hanging_server(monkeypatch):
    server = await asyncio.start_server(lambda r, w: None, host="127.0.0.1", port=0)
    host, port = server.sockets[0].getsockname()[:2]
    monkeypatch.setattr(rt, "_read_ai_base", lambda: f"http://{host}:{port}")
    monkeypatch.setattr(rt, "FETCH_TIMEOUT", 0.2)
    async with server:
        started = time.monotonic()
        listed_at, schemas = await rt.fetch_schemas("tok", 1)
        elapsed = time.monotonic() - started
    assert (listed_at, schemas) == (0, [])   # documented degrade shape, never raises
    assert elapsed < 2.0, f"fetch_schemas was not bounded by FETCH_TIMEOUT ({elapsed:.2f}s)"
