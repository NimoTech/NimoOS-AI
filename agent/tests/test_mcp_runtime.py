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
