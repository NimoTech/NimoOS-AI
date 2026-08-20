"""Direct tests for mcp_client.client._metas_for_server.

Formerly exercised only indirectly, through build_mcp_tools (see git history
of the now-deleted test_mcp_build.py / build_mcp_tools). build_mcp_tools was
deleted once its only production caller (skills/tool_gating.py's
_load_l2_tools_async) was rewritten to call _metas_for_server directly
instead of re-implementing its own, incomplete cache-check-and-fetch: that
older, ad-hoc version cache-checked correctly but built FunctionTools from
`schemas` unconditionally, ignoring the (0, [...]) "untrusted" sentinel
_metas_for_server already honours correctly (see fetched_at handling below).

_metas_for_server now has two production callers, so it gets its own direct
tests rather than only being covered as a side effect of exercising callers.
Cache-freshness edge cases (listed_at mismatch / zero-never-trusted) are
ALSO covered directly against _cache_get/_cache_put in
tests/test_mcp_cache_and_slug.py and tests/test_mcp_cache.py; the versions
here additionally pin that _metas_for_server itself respects that contract
end-to-end, not just the cache primitives in isolation.
"""
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

    async def boom(token, sid):
        raise AssertionError("must not fetch on cache hit")
    monkeypatch.setattr("mcp_client.runtime.fetch_schemas", boom)

    metas, status, detail = await mc._metas_for_server({"id": 1, "name": "x", "listed_at": 5})
    assert metas == [META]
    assert status == mc.OK
    assert detail == ""


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

    metas, status, detail = await mc._metas_for_server({"id": 1, "name": "x", "listed_at": 5})
    assert metas == [META]
    assert calls == [("tok", 1)]
    assert mc._cache_get(1, 5) is not None
    assert status == mc.OK


@pytest.mark.asyncio
async def test_schema_fetch_failure_reports_failed_and_warns(monkeypatch):
    events = []

    async def fake_emit(name, err):
        events.append(name)
    monkeypatch.setattr(mc, "_emit_warning", fake_emit)

    async def fake_fetch(token, server_id):
        return 0, []   # mcp_client.runtime.fetch_schemas's documented degrade shape
    monkeypatch.setattr("mcp_client.runtime.fetch_schemas", fake_fetch)

    metas, status, detail = await mc._metas_for_server({"id": 9, "name": "bad"})
    assert metas == [] and events == ["bad"]
    assert status == mc.FAILED
    # Pin the exact fixed string, not just truthiness — an empty-but-truthy
    # placeholder would slip past a bare `assert detail` unnoticed.
    assert detail == "could not fetch tool schemas from nimoos-ai"


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

    metas, status, detail = await mc._metas_for_server({"id": 1, "name": "x", "listed_at": 2})
    assert calls["n"] == 1
    assert metas == [META]
    assert status == mc.OK
    assert mc._cache_get(1, 2) is not None     # refetched body now cached under the new listed_at


@pytest.mark.asyncio
async def test_listed_at_zero_never_trusts_the_cache(monkeypatch):
    """A server Go has never successfully probed (or one whose runtime row was
    just deleted by an Update invalidation) reports listed_at == 0.
    _cache_get must never report a hit for that — 0 can't be a real cached
    state (see _cache_put's write guard) — so this must always fall through
    to a fresh fetch rather than serving whatever happens to be cached."""
    mc._cache_put(1, [META], listed_at=1)      # some unrelated prior good entry
    calls = {"n": 0}

    async def fake_fetch(token, server_id):
        calls["n"] += 1
        return 0, []
    monkeypatch.setattr("mcp_client.runtime.fetch_schemas", fake_fetch)

    metas, status, detail = await mc._metas_for_server({"id": 1, "name": "x", "listed_at": 0})
    assert calls["n"] == 1
    assert metas == [] and status == mc.FAILED


@pytest.mark.asyncio
async def test_fetch_returning_zero_sentinel_never_yields_trusted_metas(monkeypatch):
    """Regression pin for the Fix-2 defect: fetch_schemas degrading to
    (0, [...]) — a non-empty schemas array alongside the untrusted sentinel —
    must still be reported as FAILED/empty, never as a trusted listing. This
    combination was previously unreachable from real Go output but became
    reachable once a transport-relevant server Update deletes the runtime AND
    schemas rows together (see route/v2/mcp.go's Update handler)."""
    async def fake_fetch(token, server_id):
        return 0, [META]   # malformed/untrusted: non-empty body under the "never trust" sentinel
    monkeypatch.setattr("mcp_client.runtime.fetch_schemas", fake_fetch)

    metas, status, detail = await mc._metas_for_server({"id": 1, "name": "x", "listed_at": 0})
    assert metas == [], "fetched_at == 0 must never yield metas the caller could turn into live tools"
    assert status == mc.FAILED
