"""Manifest cache TTL policy.

This cache is not a perf optimisation — it is what makes tools RELIABLY EXIST.
The manifest is fetched at run start for every enabled server, and the cold path
gives up (returns []) rather than block startup, so a miss means the model simply
cannot see those tools this turn. The server's ttlMs is therefore an INPUT to our
policy, never a replacement for it.
"""
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
