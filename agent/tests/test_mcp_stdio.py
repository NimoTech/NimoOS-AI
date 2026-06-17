import os
import pytest
import mcp_client.client as mc


def test_stdio_env_protects_core_and_passthrough(monkeypatch):
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("SECRET_KEY", "should-not-leak")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = mc._stdio_env({"FOO": "bar", "PATH": "/evil"})
    assert env["FOO"] == "bar"            # user var kept
    assert env["LANG"] == "en_US.UTF-8"   # passthrough
    assert env["TZ"] == "UTC"
    assert env["PATH"] == "/usr/bin"      # core protected: user "/evil" did NOT win
    assert "SECRET_KEY" not in env        # agent env not bulk-inherited


@pytest.mark.asyncio
async def test_connect_stdio_branch(monkeypatch):
    captured = {}

    class FakeStdioSrv:
        def __init__(self, params=None, client_session_timeout_seconds=None, name=None):
            captured["params"] = params
            captured["timeout"] = client_session_timeout_seconds
        async def connect(self): captured["connected"] = True

    import agents.mcp as am
    monkeypatch.setattr(am, "MCPServerStdio", FakeStdioSrv, raising=False)

    server = {"id": 1, "name": "fs", "transport": "stdio",
              "command": "npx", "args": ["-y", "x"], "env": {"K": "V"}}
    conn = await mc._connect(server)
    assert captured["connected"] is True
    assert captured["params"]["command"] == "npx"
    assert captured["params"]["args"] == ["-y", "x"]
    assert captured["params"]["env"]["K"] == "V"
    assert "PATH" in captured["params"]["env"]
    assert captured["timeout"] == mc.STDIO_CONNECT_TIMEOUT


def test_connect_timeout_per_transport():
    assert mc._connect_timeout({"transport": "stdio"}) == mc.STDIO_CONNECT_TIMEOUT
    assert mc._connect_timeout({"transport": "http"}) == mc.MCP_CONNECT_TIMEOUT
    assert mc._connect_timeout({}) == mc.MCP_CONNECT_TIMEOUT
