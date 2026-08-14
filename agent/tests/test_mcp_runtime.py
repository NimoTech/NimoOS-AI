import json
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
