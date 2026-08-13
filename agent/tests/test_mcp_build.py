import time
import pytest
import mcp_client.client as mc

META = {"name": "search", "description": "d",
        "input_schema": {"type": "object", "properties": {}}}


class GoodConn:
    """Matches the new McpConn contract: call_tool / list_tools / aclose."""
    def __init__(self): self.closed = False
    async def call_tool(self, name, args): ...
    async def list_tools(self):
        return [{"name": "search", "description": "d",
                 "input_schema": {"type": "object", "properties": {}}}], mc.SCHEMA_TTL
    async def aclose(self): self.closed = True


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
    mc._cache_put(1, [META], mc._fingerprint({"id": 1, "name": "x"}), mc.SCHEMA_TTL)
    async def boom(s): raise AssertionError("must not connect on cache hit")
    monkeypatch.setattr(mc, "_connect", boom)
    tools, statuses = await mc.build_mcp_tools([{"id": 1, "name": "x"}])
    assert [t.name for t in tools] == ["mcp__x__search"]
    assert [s.status for s in statuses] == [mc.OK]
    assert statuses[0].tool_names == ["mcp__x__search"]   # fq names, post-dedup


@pytest.mark.asyncio
async def test_cold_fetch_connects_and_caches(monkeypatch):
    connects = {"n": 0}
    async def fake_connect(s):
        connects["n"] += 1
        return GoodConn()
    monkeypatch.setattr(mc, "_connect", fake_connect)
    tools, statuses = await mc.build_mcp_tools([{"id": 1, "name": "x"}])
    assert [t.name for t in tools] == ["mcp__x__search"]
    assert connects["n"] == 1
    assert mc._cache_get(1) is not None
    assert statuses[0].status == mc.OK


@pytest.mark.asyncio
async def test_cold_fetch_failure_skips(monkeypatch):
    events = []
    async def fake_emit(name, err): events.append(name)
    monkeypatch.setattr(mc, "_emit_warning", fake_emit)
    async def boom(s): raise RuntimeError("down")
    monkeypatch.setattr(mc, "_connect", boom)
    tools, statuses = await mc.build_mcp_tools([{"id": 9, "name": "bad"}])
    assert tools == [] and events == ["bad"]
    assert statuses[0].name == "bad" and statuses[0].status == mc.FAILED
    assert "down" in statuses[0].detail        # the reason reaches the model now


@pytest.mark.asyncio
async def test_stale_serves_cache_and_revalidates(monkeypatch):
    fp = mc._fingerprint({"id": 1, "name": "x"})
    mc._cache_put(1, [META], fp, mc.SCHEMA_TTL)
    mc._SCHEMA_CACHE[1].fetched_at = time.monotonic() - mc.SCHEMA_TTL - 1
    scheduled = {"n": 0}
    monkeypatch.setattr(mc, "_schedule_revalidate", lambda s: scheduled.__setitem__("n", scheduled["n"] + 1))
    async def boom(s): raise AssertionError("stale path must not connect inline")
    monkeypatch.setattr(mc, "_connect", boom)
    tools, statuses = await mc.build_mcp_tools([{"id": 1, "name": "x"}])
    assert [t.name for t in tools] == ["mcp__x__search"]
    assert scheduled["n"] == 1
    assert statuses[0].status == mc.OK         # stale-but-served counts as ok


@pytest.mark.asyncio
async def test_fingerprint_change_refetches(monkeypatch):
    mc._cache_put(1, [META], "STALE_FP", mc.SCHEMA_TTL)
    connects = {"n": 0}
    async def fake_connect(s):
        connects["n"] += 1
        return GoodConn()
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


@pytest.mark.asyncio
async def test_config_error_server_never_probed(monkeypatch):
    # Go marks servers whose stored credentials failed to decrypt (defect-1
    # silent point 3); connecting with an unauthenticated config would fail
    # with a 401 that masks the real cause.
    async def boom(s): raise AssertionError("config_error server must not be probed")
    monkeypatch.setattr(mc, "_metas_for_server", boom)
    tools, statuses = await mc.build_mcp_tools(
        [{"id": 3, "name": "broken", "config_error": "credential decryption failed"}])
    assert tools == []
    assert statuses[0].status == mc.CONFIG_ERROR
    assert "decryption" in statuses[0].detail


@pytest.mark.asyncio
async def test_mixed_servers_keep_order(monkeypatch):
    # statuses must stay aligned with the input order even when a config_error
    # server sits between two probed ones (the gather list skips it).
    async def fake_metas(s):
        return ([{"name": "t", "description": "",
                  "input_schema": {"type": "object", "properties": {}}}], mc.OK, "")
    monkeypatch.setattr(mc, "_metas_for_server", fake_metas)
    tools, statuses = await mc.build_mcp_tools([
        {"id": 1, "name": "a"},
        {"id": 2, "name": "broken", "config_error": "x"},
        {"id": 3, "name": "c"},
    ])
    assert [s.name for s in statuses] == ["a", "broken", "c"]
    assert [s.status for s in statuses] == [mc.OK, mc.CONFIG_ERROR, mc.OK]
    assert [t.name for t in tools] == ["mcp__a__t", "mcp__c__t"]
