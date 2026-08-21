"""Search backends: one shape in, one shape out.

Each backend maps the tool's uniform (days, domains) onto whatever the
provider actually supports, and reports HOW it was applied in
SearchResult.applied. That report is not cosmetic: a model told "domains
filtered" when the backend has no such filter will trust results it should
have discounted. Values are deliberately strings, not booleans —
"native" (the provider filtered), "approx" (bucketed into the provider's
coarser vocabulary), "query" (folded into the query as site: clauses),
or False (not requested).

No method raises: every failure becomes SearchResult.error, because a
search that cannot run must degrade into a message the model can read, not
an exception that kills the run. That includes an over-large response body:
reads are capped at web.fetch.MAX_BYTES (see _json_capped). This also covers a 200 response whose body
parses as JSON but has the wrong shape (top-level list, non-dict hit
entries, etc.) — see _hits_at, which degrades those to fewer/no hits
instead of raising AttributeError.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import httpx

from web import fetch as _fetch

TIMEOUT_SEC = 15.0

# --------------------------------------------------------------------------- #
# Pacing and retry for search requests.
#
# An agent loop asked to cover several source families fires its searches back
# to back. Measured on the live box: 29 `web_search` calls in one scheduled run,
# most returning `hits: 0, outcome: error`, while the same query run alone
# succeeded immediately. The provider was throttling us (Brave's free tier is
# ~1 query/second) and this module turned every 429 into an error string with no
# pacing and no retry.
#
# That failure is invisible where it matters: the tool hands the model a fenced
# error string, the model moves on, and the report says "0 items" for a source
# that was never actually searched — indistinguishable from a quiet day. And it
# gets WORSE the more thorough the task is, which is exactly backwards.
#
# Only search goes through `_json_capped` (web_fetch has its own path), so
# pacing here cannot slow page reads down.
SEARCH_MIN_INTERVAL_SEC = 1.1     # just over Brave's ~1 QPS free tier
SEARCH_ATTEMPTS = 3
SEARCH_BACKOFF_SEC = 1.0          # doubled per retry

# Retried: throttling and server-side faults. NOT retried: 401/403/404 and the
# rest of the 4xx family — a wrong API key is a configuration error, and
# retrying it triples the latency of every misconfigured install while telling
# the user nothing new.
_RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Injection seams for tests: real sleeps would make the pacing tests the slowest
# in the suite and would exercise the event loop rather than the policy.
_rate_clock = time.monotonic
_rate_sleep = asyncio.sleep

_LAST_SEARCH_AT = 0.0
_RATE_LOCK: "asyncio.Lock | None" = None


def _rate_lock() -> asyncio.Lock:
    """One lock per process, created lazily.

    Not a module-level `asyncio.Lock()`: on older Pythons that binds to the
    loop running at import time, and this module is imported long before the
    agent's loop exists.
    """
    global _RATE_LOCK
    if _RATE_LOCK is None:
        _RATE_LOCK = asyncio.Lock()
    return _RATE_LOCK


async def _pace_search() -> None:
    """Serialize search requests and space them by SEARCH_MIN_INTERVAL_SEC.

    The lock is held across the wait so two concurrent callers cannot both
    decide the interval has elapsed and fire together — which is the whole
    scenario that produced the throttling.
    """
    global _LAST_SEARCH_AT
    async with _rate_lock():
        gap = _rate_clock() - _LAST_SEARCH_AT
        if _LAST_SEARCH_AT and gap < SEARCH_MIN_INTERVAL_SEC:
            await _rate_sleep(SEARCH_MIN_INTERVAL_SEC - gap)
        _LAST_SEARCH_AT = _rate_clock()

# (upper bound in days, provider code) — take the FIRST bucket that covers days.
_BRAVE_FRESHNESS = ((1, "pd"), (7, "pw"), (31, "pm"), (366, "py"))
_SEARX_RANGE = ((1, "day"), (7, "week"), (31, "month"), (366, "year"))


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str = ""
    published: str = ""


@dataclass
class SearchResult:
    hits: list[SearchHit] = field(default_factory=list)
    applied: dict = field(default_factory=dict)
    error: str = ""


def _bucket(days: int, table) -> str:
    for limit, code in table:
        if days <= limit:
            return code
    return table[-1][1]


def _site_prefix(query: str, domains) -> str:
    """Fold a domain list into the query for backends with no native filter."""
    if not domains:
        return query
    clause = " OR ".join(f"site:{d}" for d in domains)
    return f"({clause}) {query}"


def _hits_at(doc, *keys) -> list[dict]:
    """Walk *doc* through nested dict *keys* and return the dict entries of
    the list found there.

    A 200 response with a parseable-but-wrong-shaped body (top level is a
    list instead of an object, a hit entry is a string instead of a dict,
    etc.) must not raise — it degrades to fewer/no hits instead, same as any
    other failure. Every level of the walk is guarded: a non-dict container
    at any key, a missing key, or a non-list terminal all yield [];
    non-dict entries inside the terminal list are skipped rather than
    raised on.
    """
    node = doc
    for key in keys:
        if not isinstance(node, dict):
            return []
        node = node.get(key)
    if not isinstance(node, list):
        return []
    return [x for x in node if isinstance(x, dict)]


async def _json_capped(client, method, url, **kwargs):
    """Send one request, read at most MAX_BYTES of it, parse JSON.

    Returns (doc, error) — exactly one is meaningful. r.json() buffers the whole
    body with no ceiling, and the design's 5 MB cap lived only in the fetch
    layer. A SearXNG instance is the pointy case: its address is free text an
    admin types, and a LAN instance is classified `internal` by the egress
    proxy, so neither the port policy nor TOFU stands between the agent and a
    broken (or hostile) endpoint streaming an endless body. The cap value is
    imported from web.fetch rather than duplicated, so the two layers cannot
    drift — and a test monkeypatching fetch.MAX_BYTES moves both.

    Over-cap is an error string, never an exception: the no-method-raises
    contract of this module holds.

    Paced and retried — see SEARCH_MIN_INTERVAL_SEC above for why. Retries are
    bounded and only cover the transient statuses; a persistent failure is still
    reported, never swallowed into an empty result set.
    """
    last_status_error: "httpx.HTTPStatusError | None" = None
    for attempt in range(SEARCH_ATTEMPTS):
        if attempt:
            await _rate_sleep(SEARCH_BACKOFF_SEC * (2 ** (attempt - 1)))
        await _pace_search()
        req = client.build_request(method, url, **kwargs)
        resp = await client.send(req, stream=True)
        try:
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in _RETRY_STATUSES:
                    last_status_error = exc
                    continue
                raise
            cap = _fetch.MAX_BYTES
            chunks, total = [], 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > cap:
                    return None, f"response larger than the {cap} byte limit"
                chunks.append(chunk)
        finally:
            await resp.aclose()
        return json.loads(b"".join(chunks).decode("utf-8", errors="replace")), ""
    # Out of attempts. Re-raise so the caller's own `except httpx.HTTPError`
    # formats it the same way as a first-try failure — the status has to reach
    # the user, or a throttled source is indistinguishable from an empty one.
    raise last_status_error


class TavilyBackend:
    name = "tavily"

    def __init__(self, api_key: str):
        self._key = api_key

    async def search(self, client, query, *, max_results, days,
                     domains) -> SearchResult:
        payload: dict = {"query": query, "max_results": max_results}
        if days is not None:
            payload["days"] = days
        if domains:
            payload["include_domains"] = list(domains)
        try:
            doc, err = await _json_capped(
                client, "POST", "https://api.tavily.com/search",
                json=payload,
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=TIMEOUT_SEC,
            )
        except (httpx.HTTPError, ValueError) as exc:
            return SearchResult(error=f"tavily: {exc}")
        if err:
            return SearchResult(error=f"tavily: {err}")
        hits = [
            SearchHit(title=str(x.get("title") or ""),
                      url=str(x.get("url") or ""),
                      snippet=str(x.get("content") or ""),
                      published=str(x.get("published_date") or ""))
            for x in _hits_at(doc, "results")
        ][:max_results]
        return SearchResult(hits=hits, applied={
            "days": "native" if days is not None else False,
            "domains": "native" if domains else False,
        })


class BraveBackend:
    name = "brave"

    def __init__(self, api_key: str):
        self._key = api_key

    async def search(self, client, query, *, max_results, days,
                     domains) -> SearchResult:
        params: dict = {"q": _site_prefix(query, domains), "count": max_results}
        if days is not None:
            params["freshness"] = _bucket(days, _BRAVE_FRESHNESS)
        try:
            doc, err = await _json_capped(
                client, "GET",
                "https://api.search.brave.com/res/v1/web/search",
                params=params,
                headers={"X-Subscription-Token": self._key,
                         "Accept": "application/json"},
                timeout=TIMEOUT_SEC,
            )
        except (httpx.HTTPError, ValueError) as exc:
            return SearchResult(error=f"brave: {exc}")
        if err:
            return SearchResult(error=f"brave: {err}")
        hits = [
            SearchHit(title=str(x.get("title") or ""),
                      url=str(x.get("url") or ""),
                      snippet=str(x.get("description") or ""),
                      published=str(x.get("age") or ""))
            for x in _hits_at(doc, "web", "results")
        ][:max_results]
        return SearchResult(hits=hits, applied={
            "days": "approx" if days is not None else False,
            "domains": "query" if domains else False,
        })


class SearxngBackend:
    name = "searxng"

    def __init__(self, base_url: str):
        self._base = base_url.rstrip("/")

    async def search(self, client, query, *, max_results, days,
                     domains) -> SearchResult:
        params: dict = {"q": _site_prefix(query, domains), "format": "json"}
        if days is not None:
            params["time_range"] = _bucket(days, _SEARX_RANGE)
        try:
            doc, err = await _json_capped(
                client, "GET", f"{self._base}/search", params=params,
                timeout=TIMEOUT_SEC)
        except (httpx.HTTPError, ValueError) as exc:
            return SearchResult(error=f"searxng: {exc}")
        if err:
            return SearchResult(error=f"searxng: {err}")
        hits = [
            SearchHit(title=str(x.get("title") or ""),
                      url=str(x.get("url") or ""),
                      snippet=str(x.get("content") or ""),
                      published=str(x.get("publishedDate") or ""))
            for x in _hits_at(doc, "results")
        ][:max_results]
        return SearchResult(hits=hits, applied={
            "days": "approx" if days is not None else False,
            "domains": "query" if domains else False,
        })


def build_backend(cfg: dict):
    """Return a backend instance for *cfg*, or None when it cannot run.

    Mirrors web.settings.is_configured — kept as a separate check rather than
    importing it, so this module stays usable with a hand-built dict in tests.
    """
    if not cfg.get("enabled"):
        return None
    backend = cfg.get("backend") or ""
    if backend == "tavily" and cfg.get("api_key"):
        return TavilyBackend(api_key=cfg["api_key"])
    if backend == "brave" and cfg.get("api_key"):
        return BraveBackend(api_key=cfg["api_key"])
    if backend == "searxng" and cfg.get("base_url"):
        return SearxngBackend(base_url=cfg["base_url"])
    return None
