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
    """stdio transport branch must NOT spawn the subprocess directly — it has to
    go through the netns executor (start_mcp_stdio) and connect over the Unix
    socket via netns_stdio_transport. This is a security property (sandboxed
    subprocess), not just plumbing, so it must survive the SDK-client rewrite."""
    captured = {}

    async def fake_start_mcp_stdio(command, args, env, **kwargs):
        captured["command"] = command
        captured["args"] = args
        captured["env"] = env
        captured["connect_timeout"] = kwargs.get("connect_timeout")
        return "/var/run/nimoos/agent-mcp-fake.sock"

    import netns.client as netns_client_mod
    monkeypatch.setattr(netns_client_mod, "start_mcp_stdio", fake_start_mcp_stdio)

    transport_calls = []

    def fake_netns_stdio_transport(socket_path):
        transport_calls.append(socket_path)
        return object()

    import mcp_client.netns_stdio as ns_mod
    monkeypatch.setattr(ns_mod, "netns_stdio_transport", fake_netns_stdio_transport)

    # Positive assertion that the SDK's OWN stdio transport (which spawns the
    # subprocess directly, bypassing the netns sandbox) is never even reached: an
    # implementation that went through netns AND also fell through to the SDK's
    # stdio_client would still pass every assertion above without this stub — it
    # closes exactly the gap the security property depends on.
    def _boom_sdk_stdio_client(*a, **k):
        raise AssertionError("SDK's own stdio_client must never be called — "
                              "stdio subprocesses must be spawned via the netns sandbox")

    import mcp.client.stdio as sdk_stdio_mod
    monkeypatch.setattr(sdk_stdio_mod, "stdio_client", _boom_sdk_stdio_client)

    class _FakeClientCM:
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

    monkeypatch.setattr(mc, "Client", lambda *a, **k: _FakeClientCM())

    server = {"id": 1, "name": "fs", "transport": "stdio",
              "command": "npx", "args": ["-y", "x"], "env": {"K": "V"}}
    conn = await mc._connect(server)
    try:
        # never spawned directly: the subprocess lives in the sandboxed netns
        assert captured["command"] == "npx"
        assert captured["args"] == ["-y", "x"]
        assert captured["env"]["K"] == "V"
        assert "PATH" in captured["env"]
        assert captured["connect_timeout"] == mc.STDIO_CONNECT_TIMEOUT
        assert transport_calls == ["/var/run/nimoos/agent-mcp-fake.sock"]
    finally:
        await conn.aclose()


def test_connect_timeout_per_transport():
    # NOTE: this only pins the _connect_timeout() lookup table, not enforcement.
    # MCP_CONNECT_TIMEOUT is actually ENFORCED only on the stdio branch (passed
    # straight through to netns start_mcp_stdio's connect_timeout=). For http/sse,
    # nothing currently wraps _connect() in asyncio.wait_for(..., timeout=connect_to)
    # — the handshake is bounded only by the generous httpx2 AsyncClient(timeout=
    # session_to) built in _build_transport (~60s), not this constant. Closing that
    # gap for the run-start cold path is Task 5's job (MCP_COLD_TOTAL_TIMEOUT wraps
    # connect+list together in _metas_for_server); reading this assertion as "http/sse
    # connects are capped at MCP_CONNECT_TIMEOUT today" would be wrong.
    assert mc._connect_timeout({"transport": "stdio"}) == mc.STDIO_CONNECT_TIMEOUT
    assert mc._connect_timeout({"transport": "http"}) == mc.MCP_CONNECT_TIMEOUT
    assert mc._connect_timeout({}) == mc.MCP_CONNECT_TIMEOUT


def test_session_timeout_per_transport():
    # The per-request (list/call) read timeout is decoupled from the connect cap:
    # remote tool calls (e.g. MS Learn semantic search) routinely exceed the 5s
    # connect cap, so the session timeout must be generous.
    assert mc._session_timeout({"transport": "http"}) == mc.MCP_SESSION_TIMEOUT
    assert mc._session_timeout({"transport": "sse"}) == mc.MCP_SESSION_TIMEOUT
    assert mc._session_timeout({"transport": "stdio"}) == mc.STDIO_CONNECT_TIMEOUT
    assert mc._session_timeout({}) == mc.MCP_SESSION_TIMEOUT
    assert mc.MCP_SESSION_TIMEOUT >= 30                       # room for real tool calls
    assert mc.MCP_SESSION_TIMEOUT > mc.MCP_CONNECT_TIMEOUT    # decoupled from connect cap


@pytest.mark.asyncio
async def test_connect_http_uses_session_timeout_not_connect_cap(monkeypatch):
    """Client's read_timeout_seconds (bounds every list/call JSON-RPC request) must
    be the generous MCP_SESSION_TIMEOUT, not the 5s/8s connect cap — otherwise slow
    remote tool calls get cancelled mid-call (the MS Learn 'can't call' bug).

    This no longer touches agents.mcp at all: _connect talks straight to the mcp
    2.0 SDK now, so agents.mcp.MCPServerStreamableHttp is not even imported by the
    code under test."""
    captured = {}

    async def fake_build_transport(server, stack, connect_to, session_to):
        captured["session_to"] = session_to
        return object()

    class FakeClient:
        def __init__(self, transport, **kwargs):
            captured["read_timeout_seconds"] = kwargs.get("read_timeout_seconds")
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

    monkeypatch.setattr(mc, "_build_transport", fake_build_transport)
    monkeypatch.setattr(mc, "Client", FakeClient)

    server = {"id": 2, "name": "ms-learn", "transport": "http",
              "url": "https://learn.microsoft.com/api/mcp", "headers": {}}
    conn = await mc._connect(server)
    assert captured["session_to"] == mc.MCP_SESSION_TIMEOUT
    assert captured["read_timeout_seconds"] == mc.MCP_SESSION_TIMEOUT
    await conn.aclose()


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
    assert warns and ("initializing" in warns[-1][1] or "background" in warns[-1][1])


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
async def test_cold_http_failure_schedules_self_heal(monkeypatch, _clear_cache):
    # A slow remote whose inline cold-fetch times out must NOT just give up: it
    # schedules a background revalidate (generous timeouts) so the next run gets tools.
    scheduled = {"n": 0}
    monkeypatch.setattr(mc, "_schedule_revalidate", lambda s: scheduled.__setitem__("n", scheduled["n"] + 1))
    warns = []
    async def fake_emit(name, err): warns.append((name, str(err)))
    monkeypatch.setattr(mc, "_emit_warning", fake_emit)
    async def boom_cold(s): raise TimeoutError()
    monkeypatch.setattr(mc, "_cold_fetch", boom_cold)
    metas = await mc._metas_for_server({"id": 7, "name": "h", "transport": "http", "url": "https://x"})
    assert metas == []
    assert scheduled["n"] == 1          # background self-heal scheduled
    assert warns                        # user warned


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
    assert out2["ok"] is False and "timed out" in out2["error"]


@pytest.mark.asyncio
async def test_test_server_list_tools_timeout_message(monkeypatch, _clear_cache):
    class SlowSrv:
        async def list_tools(self):
            import asyncio
            await asyncio.sleep(10)
        async def aclose(self): pass
    async def fake_connect(s, connect_timeout=None): return SlowSrv()
    monkeypatch.setattr(mc, "_connect", fake_connect)
    monkeypatch.setattr(mc, "TEST_TIMEOUT", 0.05)   # http probe budget tiny -> list times out fast
    out = await mc.test_server({"id": 1, "name": "h", "transport": "http", "url": "https://x"})
    assert out["ok"] is False and "timed out" in out["error"]


@pytest.mark.asyncio
async def test_stdio_conn_cleanup_called_on_close(monkeypatch):
    """Our run path must trigger aclose(), i.e. the AsyncExitStack unwinding in
    reverse (Client -> transport -> socket/subprocess) — which is where the stdio
    subprocess actually gets killed. Full no-orphan check is manual."""
    cleaned = {"n": 0}

    class FakeStdioSrv:
        async def aclose(self): cleaned["n"] += 1   # stack unwind happens here

    async def fake_connect(server, connect_timeout=None):
        return FakeStdioSrv()
    monkeypatch.setattr(mc, "_connect", fake_connect)

    mc._RUN_CONNS_VAR.set({})
    mc._RUN_CONN_LOCKS_VAR.set({})
    conn = await mc._get_run_conn({"id": 1, "name": "fs", "transport": "stdio", "command": "npx"})
    assert conn is not None
    await mc.close_run_conns()
    assert cleaned["n"] == 1                          # cleanup invoked exactly once
    assert mc._RUN_CONNS_VAR.get() == {}             # run conns cleared
