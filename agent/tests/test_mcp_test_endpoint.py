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
    async def fake_connect(s): return mc.McpConn(server=s, srv=GoodSrv())
    monkeypatch.setattr(mc, "_connect", fake_connect)
    out = await mc.test_server({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert out["ok"] is True and out["tool_count"] == 1 and out["tools"] == ["search"]
    assert mc._cache_get(1) is not None        # warmed


@pytest.mark.asyncio
async def test_test_server_connect_failure(monkeypatch):
    async def boom(s): raise RuntimeError("refused")
    monkeypatch.setattr(mc, "_connect", boom)
    out = await mc.test_server({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert out["ok"] is False and "连接失败" in out["error"]


@pytest.mark.asyncio
async def test_test_server_overall_timeout(monkeypatch):
    monkeypatch.setattr(mc, "TEST_TIMEOUT", 0.05)
    async def slow_connect(s):
        import asyncio
        await asyncio.sleep(1)
    monkeypatch.setattr(mc, "_connect", slow_connect)
    out = await mc.test_server({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert out["ok"] is False and "超时" in out["error"]
