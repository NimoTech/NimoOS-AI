"""web/backends.py — three search backends, uniform result shape."""
from __future__ import annotations

import httpx
import pytest

from web import backends


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_tavily_posts_key_and_maps_results():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = request.read()
        return httpx.Response(200, json={"results": [
            {"title": "T", "url": "https://a.test/1", "content": "snip",
             "published_date": "2026-08-01"}]})

    b = backends.TavilyBackend(api_key="tvly-k")
    async with _client(handler) as c:
        r = await b.search(c, "q", max_results=3, days=7, domains=["a.test"])

    assert seen["url"] == "https://api.tavily.com/search"
    assert seen["auth"] == "Bearer tvly-k"
    assert b'"days": 7' in seen["body"] or b'"days":7' in seen["body"]
    assert b"include_domains" in seen["body"]
    assert r.error == ""
    assert [h.url for h in r.hits] == ["https://a.test/1"]
    assert r.hits[0].snippet == "snip"
    assert r.applied == {"days": "native", "domains": "native"}


@pytest.mark.asyncio
async def test_brave_uses_freshness_bucket_and_folds_domains_into_query():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        seen["token"] = request.headers.get("X-Subscription-Token")
        return httpx.Response(200, json={"web": {"results": [
            {"title": "T", "url": "https://b.test/1", "description": "d",
             "age": "2 days ago"}]}})

    b = backends.BraveBackend(api_key="brv-k")
    async with _client(handler) as c:
        r = await b.search(c, "q", max_results=2, days=3,
                           domains=["b.test", "c.test"])

    assert seen["token"] == "brv-k"
    assert seen["params"]["freshness"] == "pw"      # 3 days → past week bucket
    assert seen["params"]["q"] == "(site:b.test OR site:c.test) q"
    assert r.hits[0].snippet == "d"
    assert r.applied == {"days": "approx", "domains": "query"}


@pytest.mark.asyncio
async def test_searxng_requests_json_and_truncates_to_max_results():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"results": [
            {"title": f"T{i}", "url": f"https://s.test/{i}", "content": "c"}
            for i in range(5)]})

    b = backends.SearxngBackend(base_url="http://searx.lan:8080/")
    async with _client(handler) as c:
        r = await b.search(c, "q", max_results=2, days=40, domains=["s.test"])

    assert seen["path"] == "/search"
    assert seen["params"]["format"] == "json"
    assert seen["params"]["time_range"] == "year"   # 40 days → first bucket that covers it is "year"
    assert seen["params"]["q"] == "(site:s.test) q"
    assert len(r.hits) == 2
    assert r.applied == {"days": "approx", "domains": "query"}


@pytest.mark.asyncio
async def test_tavily_top_level_list_yields_no_hits_not_exception():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    b = backends.TavilyBackend(api_key="k")
    async with _client(handler) as c:
        r = await b.search(c, "q", max_results=3, days=None, domains=None)

    assert r.hits == []
    assert r.error == ""


@pytest.mark.asyncio
async def test_tavily_skips_non_dict_hit_entries():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [
            "not-a-dict", {"title": "T", "url": "https://a.test/1"}]})

    b = backends.TavilyBackend(api_key="k")
    async with _client(handler) as c:
        r = await b.search(c, "q", max_results=3, days=None, domains=None)

    assert [h.url for h in r.hits] == ["https://a.test/1"]


@pytest.mark.asyncio
async def test_brave_top_level_list_yields_no_hits_not_exception():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    b = backends.BraveBackend(api_key="k")
    async with _client(handler) as c:
        r = await b.search(c, "q", max_results=3, days=None, domains=None)

    assert r.hits == []
    assert r.error == ""


@pytest.mark.asyncio
async def test_searxng_top_level_list_yields_no_hits_not_exception():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    b = backends.SearxngBackend(base_url="http://searx.lan:8080/")
    async with _client(handler) as c:
        r = await b.search(c, "q", max_results=3, days=None, domains=None)

    assert r.hits == []
    assert r.error == ""


@pytest.mark.asyncio
async def test_http_error_becomes_error_string_not_exception():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad key"})

    b = backends.TavilyBackend(api_key="wrong")
    async with _client(handler) as c:
        r = await b.search(c, "q", max_results=3, days=None, domains=None)

    assert r.hits == []
    assert "tavily" in r.error


@pytest.mark.asyncio
async def test_no_filters_reports_false():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    b = backends.TavilyBackend(api_key="k")
    async with _client(handler) as c:
        r = await b.search(c, "q", max_results=3, days=None, domains=None)

    assert r.applied == {"days": False, "domains": False}


def test_build_backend_picks_by_config_and_returns_none_when_unconfigured():
    assert backends.build_backend(
        {"backend": "tavily", "api_key": "k", "base_url": "", "enabled": True}
    ).name == "tavily"
    assert backends.build_backend(
        {"backend": "searxng", "api_key": "", "enabled": True,
         "base_url": "http://searx.lan:8080"}).name == "searxng"
    assert backends.build_backend(
        {"backend": "tavily", "api_key": "", "base_url": "", "enabled": True}
    ) is None
    # disabled, otherwise-complete config
    assert backends.build_backend(
        {"backend": "tavily", "api_key": "k", "base_url": "", "enabled": False}
    ) is None
    # unknown backend string
    assert backends.build_backend(
        {"backend": "duckduckgo", "api_key": "k", "base_url": "", "enabled": True}
    ) is None
    # brave with empty api_key
    assert backends.build_backend(
        {"backend": "brave", "api_key": "", "base_url": "", "enabled": True}
    ) is None
    # searxng with empty base_url
    assert backends.build_backend(
        {"backend": "searxng", "api_key": "", "base_url": "", "enabled": True}
    ) is None


@pytest.mark.asyncio
async def test_oversized_search_response_is_an_error_not_an_exception(monkeypatch):
    """r.json() used to buffer the whole body with no ceiling.

    SearXNG is the pointy case: its address is free text an admin types, and a
    LAN instance is classified `internal` by the egress proxy, so neither the
    port policy nor TOFU stands between the agent and an endpoint streaming an
    endless body. The cap comes from web.fetch, so patching it there must move
    this limit too — that is the point of not having a second constant.
    """
    from web import fetch as fetch_mod
    monkeypatch.setattr(fetch_mod, "MAX_BYTES", 64)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"results": []}' + b" " * 5000)

    b = backends.SearxngBackend(base_url="http://searx.lan:8080")
    async with _client(handler) as c:
        r = await b.search(c, "q", max_results=3, days=None, domains=None)

    assert r.hits == []
    assert "64 byte limit" in r.error
    assert r.error.startswith("searxng: ")


@pytest.mark.asyncio
async def test_under_cap_search_response_still_parses(monkeypatch):
    from web import fetch as fetch_mod
    monkeypatch.setattr(fetch_mod, "MAX_BYTES", 4096)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [
            {"title": "T", "url": "https://s.test/1", "content": "c"}]})

    b = backends.SearxngBackend(base_url="http://searx.lan:8080")
    async with _client(handler) as c:
        r = await b.search(c, "q", max_results=3, days=None, domains=None)

    assert r.error == ""
    assert [h.url for h in r.hits] == ["https://s.test/1"]
