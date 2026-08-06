"""Manifest cache TTL policy.

This cache is not a perf optimisation — it is what makes tools RELIABLY EXIST.
The manifest is fetched at run start for every enabled server, and the cold path
gives up (returns []) rather than block startup, so a miss means the model simply
cannot see those tools this turn. The server's ttlMs is therefore an INPUT to our
policy, never a replacement for it.
"""
import asyncio

import pytest

import mcp_client.client as mc


@pytest.mark.parametrize("raw_ms, expected", [
    (None, 600),        # field absent — every legacy-protocol server
    (0, 600),           # the 2026-07-28 default; converges with "absent" for free
    (-1, 600),          # defensive: a nonsense value must not shorten the window
    (500, 60),          # sub-second declaration is floored, not honoured
    (30_000, 60),       # 30s -> floor
    (60_000, 60),       # exactly the floor
    (3_600_000, 3600),  # honoured as-is
])
def test_resolve_ttl(raw_ms, expected):
    assert mc._resolve_ttl(raw_ms) == expected


def test_resolve_ttl_uses_named_constants():
    assert mc.SCHEMA_TTL == 600
    assert mc.SCHEMA_TTL_MIN == 60


def test_ttl_is_stored_at_write_time_not_recomputed_on_read():
    """The read path runs at every run start for every enabled server, so it must
    be a single subtraction — the policy is applied once, when the entry is written."""
    mc._SCHEMA_CACHE.clear()
    mc._cache_put(1, [{"name": "a"}], "fp", 90)
    assert mc._cache_get(1).ttl == 90


@pytest.mark.asyncio
async def test_short_ttl_entry_is_stale_and_triggers_single_revalidate(monkeypatch):
    """Short server-declared TTLs make background refresh much more frequent, so the
    single-flight lock goes from 'debounce' to 'stop us hammering the server'."""
    import time

    mc._SCHEMA_CACHE.clear()
    mc._REVALIDATING.clear()
    mc.EVENT_QUEUE_VAR.set(None)

    server = {"id": 1, "name": "x", "transport": "http", "url": "https://x"}
    fp = mc._fingerprint(server)
    mc._cache_put(1, [{"name": "a"}], fp, 60)
    mc._SCHEMA_CACHE[1].fetched_at = time.monotonic() - 61

    started = []

    async def fake_revalidate(s):
        started.append(s["id"])
        await asyncio.sleep(0.05)

    monkeypatch.setattr(mc, "_revalidate", fake_revalidate)

    results = await asyncio.gather(*[mc._metas_for_server(server) for _ in range(5)])
    assert all(r == [{"name": "a"}] for r in results)   # stale served, never blocked
    await asyncio.sleep(0.1)
    assert started == [1], f"single-flight broken: {len(started)} concurrent refreshes"


@pytest.mark.asyncio
async def test_cold_path_has_a_single_total_budget(monkeypatch):
    """Raising the connect leg to 8s must not double the run-start worst case.
    connect + list share ONE budget, so the cold path still gives up around 10s."""
    import time

    mc._SCHEMA_CACHE.clear()
    mc._REVALIDATING.clear()
    mc.EVENT_QUEUE_VAR.set(None)
    monkeypatch.setattr(mc, "MCP_COLD_TOTAL_TIMEOUT", 0.1)

    async def never_connects(server, connect_timeout=None):
        await asyncio.sleep(30)

    async def noop_revalidate(s):
        return None

    monkeypatch.setattr(mc, "_connect", never_connects)
    monkeypatch.setattr(mc, "_revalidate", noop_revalidate)

    started = time.monotonic()
    metas = await mc._metas_for_server(
        {"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    elapsed = time.monotonic() - started

    assert metas == []                    # give up rather than block run start
    assert elapsed < 1.0, f"cold path was not capped by the total budget ({elapsed:.2f}s)"
