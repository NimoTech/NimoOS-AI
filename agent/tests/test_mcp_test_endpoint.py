import asyncio

import pytest
import mcp_client.client as mc


class GoodConn:
    """Matches the new McpConn contract: list_tools / aclose."""
    async def list_tools(self):
        return [{"name": "search", "description": "d",
                 "input_schema": {"type": "object", "properties": {}}}], mc.SCHEMA_TTL
    async def aclose(self): pass


@pytest.fixture(autouse=True)
def _clear():
    mc._SCHEMA_CACHE.clear(); yield; mc._SCHEMA_CACHE.clear()


@pytest.mark.asyncio
async def test_test_server_ok_and_warms_cache(monkeypatch):
    async def fake_connect(s, connect_timeout=None): return GoodConn()
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
async def test_test_server_overall_timeout_still_closes_conn(monkeypatch):
    """Regression: the OUTER test_server wait_for(budget) deadline is always set
    before the INNER asyncio.wait_for(conn.list_tools(), budget) deadline -- the
    inner one only starts after connect() returns, so even with an identical
    budget its absolute deadline is strictly later. That means a slow list_tools
    times out via the OUTER wait_for, which delivers a CancelledError (a
    BaseException) into _test_server_inner -- neither its
    `except asyncio.TimeoutError` nor its `except Exception` catches that, so
    without a try/finally around the inner wait_for, conn.aclose() is never
    reached and the whole McpConn (httpx2 client / unix socket / stdio bridge)
    leaks. Must go through test_server (not call _test_server_inner directly)
    so the OUTER deadline is the one that actually fires -- calling
    _test_server_inner directly (as test_test_server_list_tools_timeout below
    does) only ever exercises the INNER timeout, which was never broken.
    """
    closed = []

    class SlowListConn:
        async def list_tools(self):
            await asyncio.sleep(10)  # much longer than TEST_TIMEOUT below
        async def aclose(self):
            closed.append(True)

    async def fake_connect(s, connect_timeout=None):
        return SlowListConn()

    monkeypatch.setattr(mc, "_connect", fake_connect)
    monkeypatch.setattr(mc, "TEST_TIMEOUT", 0.05)
    out = await mc.test_server({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert out["ok"] is False
    assert out["error_key"] == "probe_timeout"
    assert closed == [True], "conn.aclose() must be called even when the OUTER test_server deadline fires"


@pytest.mark.asyncio
async def test_test_server_list_tools_timeout(monkeypatch):
    # Drive _test_server_inner directly (not the outer test_server wait_for) so the
    # inner list_tools budget can be shrunk without racing the overall probe timeout.
    class SlowListSrv:
        async def list_tools(self):
            import asyncio
            await asyncio.sleep(1)
        async def aclose(self): pass

    async def fake_connect(s, connect_timeout=None):
        return SlowListSrv()
    monkeypatch.setattr(mc, "_connect", fake_connect)
    monkeypatch.setattr(mc, "TEST_TIMEOUT", 0.05)
    out = await mc._test_server_inner({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert out["ok"] is False
    assert out["error_key"] == "list_timeout"
    assert out["error"] == "Listing tools timed out"


@pytest.mark.asyncio
async def test_test_server_list_tools_failure(monkeypatch):
    class BoomListSrv:
        async def list_tools(self): raise RuntimeError("bad response")
        async def aclose(self): pass

    async def fake_connect(s, connect_timeout=None):
        return BoomListSrv()
    monkeypatch.setattr(mc, "_connect", fake_connect)
    out = await mc._test_server_inner({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert out["ok"] is False
    assert out["error_key"] == "list_failed"
    assert "Listing tools failed" in out["error"]
    assert out["detail"] == "bad response"
