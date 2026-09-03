"""Web tools exposed to the agent: search the web, fetch one page.

Both results are fenced with fences.fence_untrusted — a search snippet and a
page body are external text, and the model must read them as data. The fence
source names ("web-search-results", "web-page") are part of the contract:
tests assert on them, so renaming one silently drops a guardrail.

Error payloads are fenced too, via _fenced(): they quote the remote end, so
"it failed" is no safer to hand over raw than "here is the page". The single
unfenced return is the cross-host redirect notice — that one is the system
telling the model what to do next, and fencing it would say to ignore it.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from agents import function_tool

import db as _db
import tool_output as _tool_output
from audit import audit
from fences import fence_untrusted
from web import backends
from web import fetch as _fetch
from web import settings as web_settings

_SEARCH_CAP = 20000
_PAGE_CAP = 60000


def _conn():
    """Indirection so tests can point the tools at an in-memory database."""
    return _db.get_connection()


def _fenced(doc: dict, source: str, cap: int) -> str:
    """JSON-encode *doc* and wrap it in the untrusted-data fence.

    Every payload these tools hand back goes through here — success AND error.
    An error string is not the system's own words: it quotes a status line, a
    provider's error body, or a header the remote server chose. The one
    deliberate exception is the cross-host redirect notice, which IS the
    system's own instruction and stays unfenced.
    """
    text = json.dumps(doc, ensure_ascii=False)
    return fence_untrusted(source, text, cap=cap) or text


def _parse_domains(domains):
    if not domains:
        return None
    out = [d.strip() for d in str(domains).split(",") if d.strip()]
    return out or None


async def _web_search_impl(query: str, max_results: int = 5,
                           days=None, domains=None) -> str:
    cfg = web_settings.load(_conn())
    backend = backends.build_backend(cfg)
    if backend is None:
        audit("web_search", query=query, backend="", outcome="unconfigured")
        return ("web search is not configured on this NimoOS — an "
                "administrator can pick a search provider under "
                "AI settings → Web access.")
    max_results = max(1, min(int(max_results or 5), 20))
    async with httpx.AsyncClient(proxy=_fetch.PROXY_URL) as client:
        result = await backend.search(
            client, query, max_results=max_results,
            days=int(days) if days is not None else None,
            domains=_parse_domains(domains),
        )
    audit("web_search", query=query, backend=backend.name,
          hits=len(result.hits), outcome="error" if result.error else "ok")
    if result.error:
        # Fenced like the success payload: a backend error string carries text
        # the remote server chose (an HTTP status line, a provider's error
        # body), and the fence is this feature's only injection defence.
        return _fenced({"error": result.error}, "web-search-results",
                       _SEARCH_CAP)
    doc = {
        "backend": backend.name,
        "applied": result.applied,
        "results": [
            {"title": h.title, "url": h.url, "snippet": h.snippet,
             "published": h.published}
            for h in result.hits
        ],
    }
    return _fenced(doc, "web-search-results", _SEARCH_CAP)


def dedup_key(url: str) -> str:
    """Canonical form for same-run duplicate detection: normalized by
    web.fetch (scheme/host lowercased, userinfo dropped), fragment removed,
    query parameters sorted. Unparseable input is returned as-is."""
    norm, err = _fetch.normalize_url(url)
    if err:
        return (url or "").strip()
    try:
        parts = urlsplit(norm)
        query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    except ValueError:
        return norm


async def _web_fetch_impl(url: str, max_chars: int = 30000) -> str:
    max_chars = max(500, min(int(max_chars or 30000), 60000))
    # Same-run dedup (spec §4.2): the radar task fetched one 29 KB page four
    # times in a run. Only SUCCESSFUL fetches are remembered, so a 429 retry
    # still goes out. RUN_SCRATCH_VAR is unset outside an agent run → no dedup.
    scratch = _tool_output.RUN_SCRATCH_VAR.get(None)
    key = None
    if scratch is not None:
        key = ("web_fetch", dedup_key(url), max_chars)
        prev = scratch.get(key)
        if prev:
            return (f"[web_fetch skipped: this URL was already fetched in this run "
                    f"by call {prev}. Reuse that result (or the file it was saved "
                    f"to) instead of fetching it again.]")
    out = await _fetch.fetch_page(url, max_chars=max_chars)
    audit("web_fetch", url=url,
          outcome=("error" if "error" in out
                   else "redirect" if "redirect_to" in out else "ok"),
          final_url=out.get("final_url", ""))
    if "error" in out:
        # Fenced: fetch errors quote the remote server (its status line, and
        # historically its Content-Type header verbatim). Unfenced, that is a
        # direct channel into the region the system prompt tells the model to
        # trust.
        return _fenced({"error": out["error"]}, "web-page", _PAGE_CAP)
    if "redirect_to" in out:
        # Unfenced on purpose: this is OUR instruction to the model, not
        # page content. Fencing it would tell the model to ignore it.
        return (f"That URL redirects to a different host: {out['redirect_to']}\n"
                f"Nothing was fetched. If that destination is what you want, "
                f"call web_fetch again with the new URL.")
    if key is not None:
        scratch[key] = _tool_output.CALL_ID_VAR.get("") or "an earlier call"
    return _fenced(out, "web-page", _PAGE_CAP)


@function_tool
async def web_search(query: str, max_results: int = 5,
                     days: int | None = None,
                     domains: str | None = None) -> str:
    """Search the public web. Use this for anything you cannot know from
    training data — current events, today's prices, recent releases, a
    product's present documentation.

    Results are titles, URLs and snippets. When a snippet is not enough,
    follow up with web_fetch on the most promising URL.

    The `applied` field in the response reports how the filters were honored:
    "native" (the provider filtered), "approx" (bucketed into the provider's
    coarser vocabulary), "query" (folded into the query text as site:
    clauses), or false (not requested). Do not assume a filter took effect
    when it says false.

    Args:
        query: What to search for, in natural language.
        max_results: Maximum hits to return (default 5, max 20).
        days: Only results from the last N days. Omit for no time limit.
        domains: Comma-separated domains to restrict to, e.g.
            "docs.python.org,peps.python.org". Omit to search everywhere.
    """
    return await _web_search_impl(query, max_results, days, domains)


@function_tool
async def web_fetch(url: str, max_chars: int = 30000) -> str:
    """Fetch one web page and read it as markdown. Use this when the user
    gives you a URL, or after web_search to read a promising hit in full.

    Only http/https pages are supported. For PDFs and Office documents on
    the NAS, use read_document instead. If the URL redirects to a different
    host, nothing is fetched and you are told the new URL — call again with
    it if that is where you meant to go.

    The first fetch of a host the box has not seen before asks the user to
    confirm. That is expected; it is not an error.

    Args:
        url: The absolute http/https URL to read.
        max_chars: Maximum characters of page text to return (default 30000,
            max 60000).
    """
    return await _web_fetch_impl(url, max_chars)


WEB_TOOLS = [web_search, web_fetch]
