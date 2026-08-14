import pytest
import mcp_client.client as mc

META = {"name": "search", "description": "d",
        "input_schema": {"type": "object", "properties": {}}}


@pytest.fixture(autouse=True)
def _clear_cache():
    mc._SCHEMA_CACHE.clear()
    mc.EVENT_QUEUE_VAR.set(None)
    mc.WRITE_TOKEN_VAR.set("")
    yield
    mc._SCHEMA_CACHE.clear()


@pytest.mark.asyncio
async def test_cache_hit_does_not_connect(monkeypatch):
    mc._cache_put(1, [META], listed_at=5)
    async def boom(token, sid): raise AssertionError("must not fetch on cache hit")
    monkeypatch.setattr("mcp_client.runtime.fetch_schemas", boom)
    tools, statuses = await mc.build_mcp_tools([{"id": 1, "name": "x", "listed_at": 5}])
    assert [t.name for t in tools] == ["mcp__x__search"]
    assert [s.status for s in statuses] == [mc.OK]
    assert statuses[0].tool_names == ["mcp__x__search"]   # fq names, post-dedup


@pytest.mark.asyncio
async def test_cache_miss_fetches_schemas_over_loopback(monkeypatch):
    """As of Task 17, a cache miss no longer connects to the MCP server
    itself — it asks Go for the schemas it already has, over loopback,
    using this run's write token."""
    calls = []
    async def fake_fetch(token, server_id):
        calls.append((token, server_id))
        return 5, [META]
    monkeypatch.setattr("mcp_client.runtime.fetch_schemas", fake_fetch)
    mc.WRITE_TOKEN_VAR.set("tok")
    tools, statuses = await mc.build_mcp_tools([{"id": 1, "name": "x", "listed_at": 5}])
    assert [t.name for t in tools] == ["mcp__x__search"]
    assert calls == [("tok", 1)]
    assert mc._cache_get(1, 5) is not None
    assert statuses[0].status == mc.OK


@pytest.mark.asyncio
async def test_schema_fetch_failure_skips(monkeypatch):
    events = []
    async def fake_emit(name, err): events.append(name)
    monkeypatch.setattr(mc, "_emit_warning", fake_emit)
    async def fake_fetch(token, server_id):
        return 0, []   # mcp_client.runtime.fetch_schemas's documented degrade shape
    monkeypatch.setattr("mcp_client.runtime.fetch_schemas", fake_fetch)
    tools, statuses = await mc.build_mcp_tools([{"id": 9, "name": "bad"}])
    assert tools == [] and events == ["bad"]
    assert statuses[0].name == "bad" and statuses[0].status == mc.FAILED
    # Pin the exact fixed string (client.py's _metas_for_server), not just
    # truthiness — an empty-but-truthy placeholder would slip past `assert
    # statuses[0].detail` unnoticed.
    assert statuses[0].detail == "could not fetch tool schemas from nimoos-ai"


@pytest.mark.asyncio
async def test_listed_at_mismatch_forces_cold_refetch(monkeypatch):
    """The DB (mcp_server_runtime.listed_at) is the sole freshness authority now:
    once it has moved on from what this cache body was fetched under, the old
    body is a plain miss — never served stale — even though it is still sitting
    right there in _SCHEMA_CACHE."""
    mc._cache_put(1, [META], listed_at=1)
    calls = {"n": 0}
    async def fake_fetch(token, server_id):
        calls["n"] += 1
        return 2, [META]
    monkeypatch.setattr("mcp_client.runtime.fetch_schemas", fake_fetch)
    tools, statuses = await mc.build_mcp_tools([{"id": 1, "name": "x", "listed_at": 2}])
    assert calls["n"] == 1
    assert [t.name for t in tools] == ["mcp__x__search"]
    assert statuses[0].status == mc.OK
    assert mc._cache_get(1, 2) is not None     # refetched body now cached under the new listed_at


@pytest.mark.asyncio
async def test_listed_at_zero_never_trusts_the_cache(monkeypatch):
    """A server Go has never successfully probed reports listed_at == 0.
    _cache_get must never report a hit for that — 0 can't be a real cached
    state (see _cache_put's write guard) — so this must always fall through
    to a fresh fetch rather than serving whatever happens to be cached."""
    mc._cache_put(1, [META], listed_at=1)      # some unrelated prior good entry
    calls = {"n": 0}
    async def fake_fetch(token, server_id):
        calls["n"] += 1
        return 0, []
    monkeypatch.setattr("mcp_client.runtime.fetch_schemas", fake_fetch)
    await mc.build_mcp_tools([{"id": 1, "name": "x", "listed_at": 0}])
    assert calls["n"] == 1


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
async def test_two_servers_colliding_on_name_get_distinct_slugs(monkeypatch):
    """End-to-end: two servers that both slug to "github" must come out of
    build_mcp_tools as mcp__github__* and mcp__github_2__* — never
    mcp__github__search / mcp__github__search_2 (the old, deleted tool-name
    dedup), which would leave the model unable to tell the two servers apart."""
    async def fake_metas(s):
        return ([{"name": "search", "description": "",
                  "input_schema": {"type": "object", "properties": {}}}], mc.OK, "")
    monkeypatch.setattr(mc, "_metas_for_server", fake_metas)
    tools, statuses = await mc.build_mcp_tools([
        {"id": 1, "name": "GitHub"},
        {"id": 2, "name": "github"},
    ])
    assert [t.name for t in tools] == ["mcp__github__search", "mcp__github_2__search"]
    assert statuses[0].tool_names == ["mcp__github__search"]
    assert statuses[1].tool_names == ["mcp__github_2__search"]


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
