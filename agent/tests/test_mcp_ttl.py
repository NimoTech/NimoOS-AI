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
    (3_600_000, 3600),  # exactly the ceiling
    (7_200_000, 3600),  # 2h -> ceiling: a huge ttlMs must not pin a stale manifest
    (86_400_000, 3600), # 24h -> ceiling (the defect-② scenario)
])
def test_resolve_ttl(raw_ms, expected):
    assert mc._resolve_ttl(raw_ms) == expected


def test_resolve_ttl_uses_named_constants():
    assert mc.SCHEMA_TTL == 600
    assert mc.SCHEMA_TTL_MIN == 60
    assert mc.SCHEMA_TTL_MAX == 3600


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
    metas, status, detail = await mc._metas_for_server(
        {"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    elapsed = time.monotonic() - started

    assert metas == []                    # give up rather than block run start
    assert status == mc.FAILED and detail # the failure is now visible to the model
    assert elapsed < 1.0, f"cold path was not capped by the total budget ({elapsed:.2f}s)"
