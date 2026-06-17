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


@pytest.fixture
def _clear_cache():
    mc._SCHEMA_CACHE.clear(); mc._REVALIDATING.clear(); mc._BACKGROUND_TASKS.clear()
    mc.EVENT_QUEUE_VAR.set(None)
    yield
    mc._SCHEMA_CACHE.clear()


@pytest.mark.asyncio
async def test_cold_stdio_self_heals_not_inline(monkeypatch, _clear_cache):
    scheduled = {"n": 0}
    monkeypatch.setattr(mc, "_schedule_revalidate", lambda s: scheduled.__setitem__("n", scheduled["n"] + 1))
    warns = []
    async def fake_emit(name, err): warns.append((name, str(err)))
    monkeypatch.setattr(mc, "_emit_warning", fake_emit)
    async def boom(s): raise AssertionError("stdio cold must NOT connect inline")
    monkeypatch.setattr(mc, "_connect", boom)

    metas = await mc._metas_for_server({"id": 1, "name": "fs", "transport": "stdio"})
    assert metas == []
    assert scheduled["n"] == 1
    assert warns and ("初始化" in warns[-1][1] or "下载" in warns[-1][1])


@pytest.mark.asyncio
async def test_cold_http_still_inline(monkeypatch, _clear_cache):
    called = {"n": 0}
    async def fake_cold(s):
        called["n"] += 1
        return [{"name": "t", "description": "", "input_schema": {"type": "object", "properties": {}}}]
    monkeypatch.setattr(mc, "_cold_fetch", fake_cold)
    metas = await mc._metas_for_server({"id": 2, "name": "h", "transport": "http", "url": "https://x"})
    assert called["n"] == 1 and len(metas) == 1


@pytest.mark.asyncio
async def test_test_server_uses_stdio_timeout(monkeypatch, _clear_cache):
    async def fake_inner(server):
        import asyncio
        await asyncio.sleep(0.2)
        return {"ok": True, "tool_count": 0, "tools": []}
    monkeypatch.setattr(mc, "_test_server_inner", fake_inner)
    out = await mc.test_server({"id": 1, "name": "fs", "transport": "stdio", "command": "npx"})
    assert out["ok"] is True
    monkeypatch.setattr(mc, "TEST_TIMEOUT", 0.05)
    out2 = await mc.test_server({"id": 2, "name": "h", "transport": "http", "url": "https://x"})
    assert out2["ok"] is False and "超时" in out2["error"]
