import time
import pytest
import mcp_client.client as mc

META = {"name": "search", "description": "d",
        "input_schema": {"type": "object", "properties": {}}}


class GoodSrv:
    async def connect(self): pass
    async def list_tools(self):
        class T:
            name = "search"; description = "d"
            inputSchema = {"type": "object", "properties": {}}
        return [T()]
    async def call_tool(self, name, args): ...
    async def cleanup(self): pass


@pytest.fixture(autouse=True)
def _clear_cache():
    mc._SCHEMA_CACHE.clear()
    mc._REVALIDATING.clear()
    mc._BACKGROUND_TASKS.clear()
    mc.EVENT_QUEUE_VAR.set(None)
    yield
    mc._SCHEMA_CACHE.clear()


@pytest.mark.asyncio
async def test_cache_hit_does_not_connect(monkeypatch):
    mc._cache_put(1, [META], mc._fingerprint({"id": 1, "name": "x"}))
    async def boom(s): raise AssertionError("must not connect on cache hit")
    monkeypatch.setattr(mc, "_connect", boom)
    tools = await mc.build_mcp_tools([{"id": 1, "name": "x"}])
    assert [t.name for t in tools] == ["mcp__x__search"]


@pytest.mark.asyncio
async def test_cold_fetch_connects_and_caches(monkeypatch):
    connects = {"n": 0}
    async def fake_connect(s):
        connects["n"] += 1
        return mc.McpConn(server=s, srv=GoodSrv())
    monkeypatch.setattr(mc, "_connect", fake_connect)
    tools = await mc.build_mcp_tools([{"id": 1, "name": "x"}])
    assert [t.name for t in tools] == ["mcp__x__search"]
    assert connects["n"] == 1
    assert mc._cache_get(1) is not None


@pytest.mark.asyncio
async def test_cold_fetch_failure_skips(monkeypatch):
    events = []
    async def fake_emit(name, err): events.append(name)
    monkeypatch.setattr(mc, "_emit_warning", fake_emit)
    async def boom(s): raise RuntimeError("down")
    monkeypatch.setattr(mc, "_connect", boom)
    tools = await mc.build_mcp_tools([{"id": 9, "name": "bad"}])
    assert tools == [] and events == ["bad"]


@pytest.mark.asyncio
async def test_stale_serves_cache_and_revalidates(monkeypatch):
    fp = mc._fingerprint({"id": 1, "name": "x"})
    mc._cache_put(1, [META], fp)
    mc._SCHEMA_CACHE[1].fetched_at = time.monotonic() - mc.SCHEMA_TTL - 1
    scheduled = {"n": 0}
    monkeypatch.setattr(mc, "_schedule_revalidate", lambda s: scheduled.__setitem__("n", scheduled["n"] + 1))
    async def boom(s): raise AssertionError("stale path must not connect inline")
    monkeypatch.setattr(mc, "_connect", boom)
    tools = await mc.build_mcp_tools([{"id": 1, "name": "x"}])
    assert [t.name for t in tools] == ["mcp__x__search"]
    assert scheduled["n"] == 1


@pytest.mark.asyncio
async def test_fingerprint_change_refetches(monkeypatch):
    mc._cache_put(1, [META], "STALE_FP")
    connects = {"n": 0}
    async def fake_connect(s):
        connects["n"] += 1
        return mc.McpConn(server=s, srv=GoodSrv())
    monkeypatch.setattr(mc, "_connect", fake_connect)
    await mc.build_mcp_tools([{"id": 1, "name": "x", "url": "https://new"}])
    assert connects["n"] == 1


@pytest.mark.asyncio
async def test_schedule_revalidate_single_flight(monkeypatch):
    calls = {"n": 0}
    async def fake_revalidate(s):
        calls["n"] += 1
        import asyncio
        await asyncio.sleep(0.02)
    monkeypatch.setattr(mc, "_revalidate", fake_revalidate)
    mc._REVALIDATING.clear()
    for _ in range(5):
        mc._schedule_revalidate({"id": 1, "name": "x"})
    import asyncio
    await asyncio.sleep(0.05)
    assert calls["n"] == 1
    assert 1 not in mc._REVALIDATING
    assert len(mc._BACKGROUND_TASKS) == 0
