"""web/fetch.py — normalization, redirect policy, caps, cache."""
from __future__ import annotations

import httpx
import pytest

from web import fetch


@pytest.fixture(autouse=True)
def _clean_cache():
    fetch.clear_cache()
    yield
    fetch.clear_cache()


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_normalize_upgrades_http_and_strips_credentials():
    url, err = fetch.normalize_url("http://user:pw@Example.test/a?b=1")
    assert err == ""
    assert url.startswith("https://example.test/a")
    assert "user" not in url and "pw" not in url


def test_normalize_rejects_non_http_scheme():
    for raw in ("file:///etc/passwd", "ftp://x.test/a", "javascript:alert(1)"):
        url, err = fetch.normalize_url(raw)
        assert url == "" and err, raw


def test_normalize_rejects_overlong_url():
    url, err = fetch.normalize_url("https://x.test/" + "a" * 2100)
    assert url == ""
    assert "too long" in err


def test_normalize_rejects_missing_host():
    url, err = fetch.normalize_url("https:///nohost")
    assert url == "" and err


@pytest.mark.asyncio
async def test_fetch_returns_markdown_and_title():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html="<html><head><title>T</title></head>"
                                        "<body><p>hello body</p></body></html>")

    async with _client(handler) as c:
        out = await fetch.fetch_page("https://x.test/a", client=c)

    assert out["title"] == "T"
    assert "hello body" in out["content_markdown"]
    assert out["truncated"] is False
    assert out["final_url"] == "https://x.test/a"


@pytest.mark.asyncio
async def test_cross_host_redirect_is_reported_not_followed():
    hits = []

    async def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://other.test/b"})

    async with _client(handler) as c:
        out = await fetch.fetch_page("https://x.test/a", client=c)

    assert out["redirect_to"] == "https://other.test/b"
    assert "content_markdown" not in out
    assert hits == ["https://x.test/a"]      # never dialed the second host


@pytest.mark.asyncio
async def test_same_host_redirect_is_followed():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a":
            return httpx.Response(301, headers={"Location": "/b"})
        return httpx.Response(200, html="<html><body><p>landed</p></body></html>")

    async with _client(handler) as c:
        out = await fetch.fetch_page("https://x.test/a", client=c)

    assert "landed" in out["content_markdown"]
    assert out["final_url"] == "https://x.test/b"


@pytest.mark.asyncio
async def test_unsupported_content_type_is_refused():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"%PDF-1.7",
                              headers={"Content-Type": "application/pdf"})

    async with _client(handler) as c:
        out = await fetch.fetch_page("https://x.test/a.pdf", client=c)

    assert "error" in out
    assert "read_document" in out["error"]


@pytest.mark.asyncio
async def test_max_chars_truncates_and_flags():
    body = "<html><body><p>" + ("z" * 500) + "</p></body></html>"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html=body)

    async with _client(handler) as c:
        out = await fetch.fetch_page("https://x.test/a", max_chars=100, client=c)

    assert out["truncated"] is True
    assert len(out["content_markdown"]) <= 100


@pytest.mark.asyncio
async def test_second_call_is_served_from_cache():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, html="<html><body><p>once</p></body></html>")

    async with _client(handler) as c:
        a = await fetch.fetch_page("https://x.test/a", client=c)
        b = await fetch.fetch_page("https://x.test/a", client=c)

    assert calls == 1
    assert a == b


@pytest.mark.asyncio
async def test_proxy_unreachable_is_a_clear_error_not_a_hang():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("all connection attempts failed")

    async with _client(handler) as c:
        out = await fetch.fetch_page("https://x.test/a", client=c)

    assert "error" in out
    assert "egress proxy" in out["error"]


def test_normalize_rejects_malformed_url_without_raising():
    for raw in ("https://x.test:99999999/a",
                "https://x.test:abc/a",
                "https://[::1/a"):
        url, err = fetch.normalize_url(raw)
        assert url == "" and err, raw


@pytest.mark.asyncio
async def test_max_bytes_cap_truncates_streamed_body(monkeypatch):
    monkeypatch.setattr(fetch, "MAX_BYTES", 100)
    body = "<html><body><p>" + ("z" * 5000) + "</p></body></html>"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html=body)

    async with _client(handler) as c:
        out = await fetch.fetch_page("https://x.test/a", client=c)

    assert "content_markdown" in out
    assert len(out["content_markdown"]) < 5000


@pytest.mark.asyncio
async def test_same_host_redirect_upgrades_scheme_back_to_https():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a":
            return httpx.Response(301, headers={"Location": "http://x.test/b"})
        assert request.url.scheme == "https"
        return httpx.Response(200, html="<html><body><p>landed</p></body></html>")

    async with _client(handler) as c:
        out = await fetch.fetch_page("https://x.test/a", client=c)

    assert out["final_url"] == "https://x.test/b"
    assert "landed" in out["content_markdown"]


@pytest.mark.asyncio
async def test_error_responses_are_not_cached():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async with _client(handler) as c:
        a = await fetch.fetch_page("https://x.test/err", client=c)
        b = await fetch.fetch_page("https://x.test/err", client=c)

    assert "error" in a and "error" in b
    assert calls == 2


@pytest.mark.asyncio
async def test_cross_host_redirect_is_not_cached():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": "https://other.test/b"})

    async with _client(handler) as c:
        a = await fetch.fetch_page("https://x.test/redir", client=c)
        b = await fetch.fetch_page("https://x.test/redir", client=c)

    assert a["redirect_to"] == "https://other.test/b"
    assert b["redirect_to"] == "https://other.test/b"
    assert calls == 2
