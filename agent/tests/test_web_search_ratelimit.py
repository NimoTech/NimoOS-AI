"""Search requests must be paced and retried, or an agent loop starves itself.

Found on the live box. A scheduled task told to cover six source families fired
29 `web_search` calls back to back; most came back `hits: 0, outcome: error`
while the exact same query run alone returned results immediately. The provider
was rate-limiting us (Brave's free tier is ~1 query/second) and
`_json_capped` turned each 429 into an error string with no retry and no pacing.

Two things make that failure especially bad:

* it is INVISIBLE. The tool returns a fenced error string, the model reads one
  line, shrugs, and moves on — so the report says "0 条" for a source that was
  never actually searched. The user cannot tell "nothing happened today" from
  "we were throttled".
* it scales with thoroughness. The more sources a task is asked to cover, the
  more of them fail. Exactly backwards.

So: serialize search requests behind a minimum interval, and retry the
transient statuses. A 401/403 is NOT transient — a wrong API key must fail on
the first try rather than after three, or every misconfiguration costs the user
three times the latency and the log says nothing useful.
"""
from __future__ import annotations

import httpx
import pytest

from web import backends


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _fast_clock(monkeypatch):
    """Deterministic pacing: a fake clock that only advances when we sleep.

    Real sleeps would make this file the slowest in the suite and would test
    the event loop rather than the policy.
    """
    state = {"now": 1000.0, "slept": []}

    async def fake_sleep(seconds):
        state["slept"].append(seconds)
        state["now"] += seconds

    monkeypatch.setattr(backends, "_rate_clock", lambda: state["now"])
    monkeypatch.setattr(backends, "_rate_sleep", fake_sleep)
    monkeypatch.setattr(backends, "_LAST_SEARCH_AT", 0.0, raising=False)
    return state


# ── retry on transient failures ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_429_is_retried_and_the_next_attempt_can_succeed():
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "slow down"})
        return httpx.Response(200, json={"web": {"results": [
            {"title": "T", "url": "https://a.test/1", "description": "s"}]}})

    b = backends.BraveBackend(api_key="k")
    async with _client(handler) as c:
        r = await b.search(c, "q", max_results=3, days=3, domains=None)

    assert calls["n"] == 2
    assert r.error == ""
    assert [h.url for h in r.hits] == ["https://a.test/1"]


@pytest.mark.asyncio
async def test_a_500_is_retried():
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="upstream down")
        return httpx.Response(200, json={"web": {"results": []}})

    b = backends.BraveBackend(api_key="k")
    async with _client(handler) as c:
        r = await b.search(c, "q", max_results=3, days=None, domains=None)

    assert calls["n"] == 3
    assert r.error == ""


@pytest.mark.asyncio
async def test_retries_are_bounded_and_the_error_survives():
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return httpx.Response(429, text="nope")

    b = backends.BraveBackend(api_key="k")
    async with _client(handler) as c:
        r = await b.search(c, "q", max_results=3, days=None, domains=None)

    assert calls["n"] == backends.SEARCH_ATTEMPTS
    assert r.error, "a persistent 429 must still be reported, not swallowed"
    assert "429" in r.error


@pytest.mark.asyncio
async def test_a_bad_api_key_fails_on_the_first_attempt():
    # 401/403 is a configuration error. Retrying it triples the latency of every
    # misconfigured install and tells the user nothing new.
    for status in (401, 403):
        calls = {"n": 0}

        async def handler(request, _s=status):
            calls["n"] += 1
            return httpx.Response(_s, text="denied")

        b = backends.BraveBackend(api_key="wrong")
        async with _client(handler) as c:
            r = await b.search(c, "q", max_results=3, days=None, domains=None)

        assert calls["n"] == 1, f"status {status} must not be retried"
        assert r.error


@pytest.mark.asyncio
async def test_backoff_grows_between_attempts(_fast_clock):
    async def handler(request):
        return httpx.Response(429, text="nope")

    b = backends.BraveBackend(api_key="k")
    async with _client(handler) as c:
        await b.search(c, "q", max_results=3, days=None, domains=None)

    # Sleeps include pacing gaps; the retry backoffs must be increasing.
    backoffs = [s for s in _fast_clock["slept"] if s >= backends.SEARCH_BACKOFF_SEC]
    assert len(backoffs) >= 2
    assert backoffs[1] > backoffs[0]


# ── pacing between consecutive searches ──────────────────────────────────────

@pytest.mark.asyncio
async def test_the_first_search_is_not_delayed(_fast_clock):
    async def handler(request):
        return httpx.Response(200, json={"web": {"results": []}})

    b = backends.BraveBackend(api_key="k")
    async with _client(handler) as c:
        await b.search(c, "q", max_results=3, days=None, domains=None)

    assert _fast_clock["slept"] == [], "nothing to wait for on the first call"


@pytest.mark.asyncio
async def test_back_to_back_searches_are_spaced_out(_fast_clock):
    async def handler(request):
        return httpx.Response(200, json={"web": {"results": []}})

    b = backends.BraveBackend(api_key="k")
    async with _client(handler) as c:
        await b.search(c, "q1", max_results=3, days=None, domains=None)
        await b.search(c, "q2", max_results=3, days=None, domains=None)
        await b.search(c, "q3", max_results=3, days=None, domains=None)

    paced = [s for s in _fast_clock["slept"] if s > 0]
    assert len(paced) == 2, "calls 2 and 3 each wait; call 1 does not"
    for gap in paced:
        assert gap <= backends.SEARCH_MIN_INTERVAL_SEC + 1e-6


@pytest.mark.asyncio
async def test_a_search_after_a_long_pause_is_not_delayed(_fast_clock):
    async def handler(request):
        return httpx.Response(200, json={"web": {"results": []}})

    b = backends.BraveBackend(api_key="k")
    async with _client(handler) as c:
        await b.search(c, "q1", max_results=3, days=None, domains=None)
        _fast_clock["now"] += 60          # a minute of thinking happened
        await b.search(c, "q2", max_results=3, days=None, domains=None)

    assert _fast_clock["slept"] == [], "the interval had already elapsed"


@pytest.mark.asyncio
async def test_the_interval_is_at_least_one_second():
    # Brave's free tier is ~1 query/second; a shorter interval would not fix
    # the very failure this exists for.
    assert backends.SEARCH_MIN_INTERVAL_SEC >= 1.0
