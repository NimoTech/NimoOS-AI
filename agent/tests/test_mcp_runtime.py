import json
from mcp_client.runtime import parse_servers, AI_URL_PATH


def test_parse_servers_valid():
    payload = json.dumps({"servers": [
        {"id": 1, "name": "github", "transport": "http", "url": "https://x",
         "command": "", "args": [], "env": {}, "headers": {"Authorization": "Bearer x"}}
    ]})
    servers = parse_servers(payload)
    assert len(servers) == 1
    assert servers[0]["name"] == "github"
    assert servers[0]["headers"]["Authorization"] == "Bearer x"


def test_parse_servers_garbage_returns_empty():
    assert parse_servers("not json") == []
    assert parse_servers(json.dumps({"unexpected": 1})) == []


def test_ai_url_path_constant():
    assert AI_URL_PATH.endswith("/var/run/nimoos/ai.url")
