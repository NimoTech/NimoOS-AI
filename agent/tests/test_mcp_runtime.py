import json
import pytest

import mcp_client.runtime as rt
from mcp_client.runtime import (AI_URL_PATH, ConfigUnavailable, fetch_mcp_servers,
                                parse_servers)


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


@pytest.mark.asyncio
async def test_fetch_without_ticket_is_config_unavailable():
    out = await fetch_mcp_servers("")
    assert isinstance(out, ConfigUnavailable) and out.reason


@pytest.mark.asyncio
async def test_fetch_without_ai_url_is_config_unavailable(monkeypatch):
    monkeypatch.setattr(rt, "_read_ai_base", lambda: None)
    out = await fetch_mcp_servers("tok")
    assert isinstance(out, ConfigUnavailable) and "ai.url" in out.reason


@pytest.mark.asyncio
async def test_fetch_non_200_status_is_config_unavailable(monkeypatch):
    # Mock _read_ai_base to return a fake base
    monkeypatch.setattr(rt, "_read_ai_base", lambda: "http://127.0.0.1:1")

    # Create a fake response object with status_code=500
    class FakeResponse:
        status_code = 500

    # Create a fake AsyncClient that returns the fake response
    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return False

        async def get(self, *args, **kwargs):
            return FakeResponse()

    # Mock httpx.AsyncClient
    monkeypatch.setattr(rt.httpx, "AsyncClient", FakeAsyncClient)

    out = await fetch_mcp_servers("tok")
    assert isinstance(out, ConfigUnavailable) and "500" in out.reason


@pytest.mark.asyncio
async def test_fetch_request_exception_is_config_unavailable(monkeypatch):
    # Mock _read_ai_base to return a fake base
    monkeypatch.setattr(rt, "_read_ai_base", lambda: "http://127.0.0.1:1")

    # Create a fake AsyncClient that raises an exception on get()
    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return False

        async def get(self, *args, **kwargs):
            raise RuntimeError("boom")

    # Mock httpx.AsyncClient
    monkeypatch.setattr(rt.httpx, "AsyncClient", FakeAsyncClient)

    out = await fetch_mcp_servers("tok")
    assert isinstance(out, ConfigUnavailable) and "boom" in out.reason
