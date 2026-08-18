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
an exception that kills the run. This also covers a 200 response whose body
parses as JSON but has the wrong shape (top-level list, non-dict hit
entries, etc.) — see _hits_at, which degrades those to fewer/no hits
instead of raising AttributeError.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx

TIMEOUT_SEC = 15.0

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
            r = await client.post(
                "https://api.tavily.com/search",
                json=payload,
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=TIMEOUT_SEC,
            )
            r.raise_for_status()
            doc = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            return SearchResult(error=f"tavily: {exc}")
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
            r = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params=params,
                headers={"X-Subscription-Token": self._key,
                         "Accept": "application/json"},
                timeout=TIMEOUT_SEC,
            )
            r.raise_for_status()
            doc = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            return SearchResult(error=f"brave: {exc}")
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
            r = await client.get(f"{self._base}/search", params=params,
                                 timeout=TIMEOUT_SEC)
            r.raise_for_status()
            doc = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            return SearchResult(error=f"searxng: {exc}")
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
