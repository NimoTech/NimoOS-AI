import time
import mcp_client.client as mc


def test_fingerprint_stable_and_sensitive():
    a = {"transport": "http", "url": "https://x", "headers": {"A": "1"}}
    b = {"transport": "http", "url": "https://x", "headers": {"A": "1"}}
    c = {"transport": "http", "url": "https://x", "headers": {"A": "2"}}
    assert mc._fingerprint(a) == mc._fingerprint(b)
    assert mc._fingerprint(a) != mc._fingerprint(c)


def test_extract_meta():
    class T:
        name = "search"; description = "d"
        inputSchema = {"type": "object", "properties": {"q": {"type": "string"}}}
    m = mc._extract_meta(T())
    assert m == {"name": "search", "description": "d",
                 "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}}


def test_cache_put_get_lru(monkeypatch):
    mc._SCHEMA_CACHE.clear()
    monkeypatch.setattr(mc, "SCHEMA_CACHE_MAX", 2)
    mc._cache_put(1, [{"name": "a"}], "fp1", mc.SCHEMA_TTL)
    mc._cache_put(2, [{"name": "b"}], "fp2", mc.SCHEMA_TTL)
    assert mc._cache_get(1) is not None          # touch 1 -> most-recent
    mc._cache_put(3, [{"name": "c"}], "fp3", mc.SCHEMA_TTL)      # over cap -> evict LRU (id 2)
    assert mc._cache_get(2) is None
    assert mc._cache_get(1) is not None
    assert mc._cache_get(3) is not None
    e = mc._cache_get(1)
    assert e.metas == [{"name": "a"}] and e.fingerprint == "fp1"
    assert e.ttl == mc.SCHEMA_TTL
