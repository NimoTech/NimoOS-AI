import mcp_client.client as mc


def test_cache_invalidates_when_listed_at_advances():
    """Design doc §1.2.1, key link 2: the in-process cache validates itself
    against listed_at. Judging freshness by TTL alone would mean a changed
    tool description could never reach the model's context."""
    mc._SCHEMA_CACHE.clear()
    mc._cache_put(1, [{"name": "a"}], listed_at=100)
    assert mc._cache_get(1, 100) is not None
    assert mc._cache_get(1, 200) is None, "a newer listed_at must invalidate the cached body"


def test_cache_entry_has_no_ttl_or_fingerprint():
    """Freshness has exactly one authority (the DB). Keeping a second set of
    freshness books here would let them diverge."""
    mc._SCHEMA_CACHE.clear()
    mc._cache_put(1, [], listed_at=1)
    entry = mc._SCHEMA_CACHE[1]
    for gone in ("ttl", "fingerprint", "fetched_at"):
        assert not hasattr(entry, gone), f"_CacheEntry must not carry {gone} anymore"


def test_cache_put_skips_when_listed_at_is_zero():
    """fetch_schemas degrades to (0, []) whenever it cannot trust the response
    (Task 12). listed_at == 0 must never be written as a new cache state —
    otherwise one failed fetch right after a good one would silently replace
    a valid cached manifest with an empty one."""
    mc._SCHEMA_CACHE.clear()
    mc._cache_put(1, [{"name": "a"}], listed_at=5)
    mc._cache_put(1, [], listed_at=0)
    entry = mc._cache_get(1, 5)
    assert entry is not None and entry.metas == [{"name": "a"}], \
        "listed_at=0 must be a no-op, not an overwrite of the last good entry"


def test_dedup_happens_at_slug_level():
    """Two servers that both slug to "github" must produce mcp__github__* and
    mcp__github_2__* — not mcp__github__create_issue and
    mcp__github__create_issue_2, which would leave the model unable to tell
    which server it is calling, and break the correspondence between the
    gate a user opens (mcp:github_2) and the tools that gate exposes."""
    slugs = mc.assign_slugs([{"id": 1, "name": "GitHub"}, {"id": 2, "name": "github"}])
    assert slugs == {1: "github", 2: "github_2"}
