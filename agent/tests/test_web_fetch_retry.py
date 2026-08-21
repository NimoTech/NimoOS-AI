"""web_fetch must retry a throttled host, not report it as empty.

Found on the live box, one layer down from the search fix. A scheduled task
fetched four Reddit search feeds in quick succession and three came back
HTTP 429 — Reddit throttles a datacenter IP hard. The pacing added to
`web/backends.py` covers SEARCH only (`_json_capped`); `web_fetch` had no
retry at all, so a throttled feed became "0 items" in the report.

That is the same invisible-failure shape the search fix exists for: the reader
cannot tell "Reddit had nothing about Beelink today" from "we were rate
limited". And it hits hardest exactly when a task is asked to cover more
sources, because more sources means more requests to the same host.

Retrying is enough here — no global pacing. Ten YouTube channel feeds fetched
back to back all succeeded, so serializing every host behind one interval would
slow the common case to fix the uncommon one. `Retry-After` is honoured when the
server sends it, because a server that names its own cooldown knows better than
our backoff curve.
"""
from __future__ import annotations

import httpx
import pytest

from web import fetch as wfetch


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(wfetch, "_retry_sleep", fake_sleep)
    return slept


_OK_BODY = b"<html><body><p>hello</p></body></html>"


def _ok():
    return httpx.Response(200, content=_OK_BODY,
                          headers={"Content-Type": "text/html"})


@pytest.mark.asyncio
async def test_a_429_is_retried_and_the_next_attempt_can_succeed():
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow down")
        return _ok()

    async with _client(handler) as c:
        got = await wfetch._fetch_once(c, "https://www.reddit.com/search.rss?q=x")

    assert calls["n"] == 2
    assert "error" not in got, got
    assert "hello" in got["_body"]


@pytest.mark.asyncio
async def test_a_503_is_retried():
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return _ok() if calls["n"] >= 3 else httpx.Response(503, text="down")

    async with _client(handler) as c:
        got = await wfetch._fetch_once(c, "https://example.test/x")

    assert calls["n"] == 3
    assert "error" not in got, got


@pytest.mark.asyncio
async def test_retries_are_bounded_and_the_status_is_still_reported():
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return httpx.Response(429, text="nope")

    async with _client(handler) as c:
        got = await wfetch._fetch_once(c, "https://example.test/x")

    assert calls["n"] == wfetch.FETCH_ATTEMPTS
    # A throttled source must never look like an empty one.
    assert "error" in got
    assert "429" in got["error"]


@pytest.mark.asyncio
async def test_a_404_is_not_retried():
    # A missing page is a fact, not a hiccup. Retrying it triples the latency
    # of every dead link a model follows.
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return httpx.Response(404, text="gone")

    async with _client(handler) as c:
        got = await wfetch._fetch_once(c, "https://example.test/missing")

    assert calls["n"] == 1
    assert "error" in got


@pytest.mark.asyncio
async def test_a_redirect_is_not_retried():
    # Cross-host redirects are reported to the caller by design; retrying would
    # just repeat the same redirect.
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return httpx.Response(302, headers={"Location": "https://other.test/y"})

    async with _client(handler) as c:
        got = await wfetch._fetch_once(c, "https://example.test/x")

    assert calls["n"] == 1
    assert got.get("_redirect") == "https://other.test/y"


@pytest.mark.asyncio
async def test_retry_after_is_honoured_when_the_server_sends_one(_no_real_sleeping):
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, text="wait")
        return _ok()

    async with _client(handler) as c:
        await wfetch._fetch_once(c, "https://example.test/x")

    assert 7 in _no_real_sleeping, _no_real_sleeping


@pytest.mark.asyncio
async def test_an_absurd_retry_after_is_capped(_no_real_sleeping):
    # A hostile or broken server must not be able to park the run for an hour.
    async def handler(request):
        return httpx.Response(429, headers={"Retry-After": "99999"}, text="wait")

    async with _client(handler) as c:
        await wfetch._fetch_once(c, "https://example.test/x")

    assert _no_real_sleeping
    assert max(_no_real_sleeping) <= wfetch.FETCH_MAX_BACKOFF_SEC


@pytest.mark.asyncio
async def test_a_garbage_retry_after_falls_back_to_the_backoff_curve(_no_real_sleeping):
    async def handler(request):
        return httpx.Response(429, headers={"Retry-After": "tomorrow"}, text="wait")

    async with _client(handler) as c:
        got = await wfetch._fetch_once(c, "https://example.test/x")

    assert "error" in got            # still bounded, still reported
    assert _no_real_sleeping         # and it did back off rather than spin


@pytest.mark.asyncio
async def test_a_successful_first_attempt_never_sleeps(_no_real_sleeping):
    async def handler(request):
        return _ok()

    async with _client(handler) as c:
        got = await wfetch._fetch_once(c, "https://example.test/x")

    assert "error" not in got
    assert _no_real_sleeping == []


@pytest.mark.asyncio
async def test_the_backoff_grows_across_attempts(_no_real_sleeping):
    # A host that keeps throttling should get progressively more room, not the
    # same short pause three times.
    async def handler(request):
        return httpx.Response(429, text="nope")

    async with _client(handler) as c:
        await wfetch._fetch_once(c, "https://example.test/x")

    assert len(_no_real_sleeping) >= 2
    assert _no_real_sleeping[1] > _no_real_sleeping[0]
