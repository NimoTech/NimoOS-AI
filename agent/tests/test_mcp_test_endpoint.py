import pytest
import mcp_client.client as mc


class GoodSrv:
    async def connect(self): pass
    async def list_tools(self):
        class T:
            name = "search"; description = "d"
            inputSchema = {"type": "object", "properties": {}}
        return [T()]
    async def cleanup(self): pass


@pytest.fixture(autouse=True)
def _clear():
    mc._SCHEMA_CACHE.clear(); yield; mc._SCHEMA_CACHE.clear()


@pytest.mark.asyncio
async def test_test_server_ok_and_warms_cache(monkeypatch):
    async def fake_connect(s, connect_timeout=None): return mc.McpConn(server=s, srv=GoodSrv())
    monkeypatch.setattr(mc, "_connect", fake_connect)
    out = await mc.test_server({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert out["ok"] is True and out["tool_count"] == 1 and out["tools"] == ["search"]
    assert mc._cache_get(1) is not None        # warmed


@pytest.mark.asyncio
async def test_test_server_connect_failure(monkeypatch):
    async def boom(s, connect_timeout=None): raise RuntimeError("refused")
    monkeypatch.setattr(mc, "_connect", boom)
    out = await mc.test_server({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert out["ok"] is False
    assert out["error_key"] == "connect_failed"
    assert "Connection failed" in out["error"]
    assert out["detail"] == "refused"


@pytest.mark.asyncio
async def test_test_server_overall_timeout(monkeypatch):
    monkeypatch.setattr(mc, "TEST_TIMEOUT", 0.05)
    async def slow_connect(s, connect_timeout=None):
        import asyncio
        await asyncio.sleep(1)
    monkeypatch.setattr(mc, "_connect", slow_connect)
    out = await mc.test_server({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert out["ok"] is False
    assert out["error_key"] == "probe_timeout"
    assert out["error"] == "Probe timed out"


@pytest.mark.asyncio
async def test_test_server_list_tools_timeout(monkeypatch):
    # Drive _test_server_inner directly (not the outer test_server wait_for) so the
    # inner list_tools budget can be shrunk without racing the overall probe timeout.
    class SlowListSrv:
        async def connect(self): pass
        async def list_tools(self):
            import asyncio
            await asyncio.sleep(1)
        async def cleanup(self): pass

    async def fake_connect(s, connect_timeout=None):
        return mc.McpConn(server=s, srv=SlowListSrv())
    monkeypatch.setattr(mc, "_connect", fake_connect)
    monkeypatch.setattr(mc, "TEST_TIMEOUT", 0.05)
    out = await mc._test_server_inner({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert out["ok"] is False
    assert out["error_key"] == "list_timeout"
    assert out["error"] == "Listing tools timed out"


@pytest.mark.asyncio
async def test_test_server_list_tools_failure(monkeypatch):
    class BoomListSrv:
        async def connect(self): pass
        async def list_tools(self): raise RuntimeError("bad response")
        async def cleanup(self): pass

    async def fake_connect(s, connect_timeout=None):
        return mc.McpConn(server=s, srv=BoomListSrv())
    monkeypatch.setattr(mc, "_connect", fake_connect)
    out = await mc._test_server_inner({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert out["ok"] is False
    assert out["error_key"] == "list_failed"
    assert "Listing tools failed" in out["error"]
    assert out["detail"] == "bad response"
