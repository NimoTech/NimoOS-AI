"""Web tools exposed to the agent: search the web, fetch one page.

Both results are fenced with fences.fence_untrusted — a search snippet and a
page body are external text, and the model must read them as data. The fence
source names ("web-search-results", "web-page") are part of the contract:
tests assert on them, so renaming one silently drops a guardrail.
"""
from __future__ import annotations

import json

import httpx
from agents import function_tool

import db as _db
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
        return json.dumps({"error": result.error}, ensure_ascii=False)
    doc = {
        "backend": backend.name,
        "applied": result.applied,
        "results": [
            {"title": h.title, "url": h.url, "snippet": h.snippet,
             "published": h.published}
            for h in result.hits
        ],
    }
    text = json.dumps(doc, ensure_ascii=False)
    return fence_untrusted("web-search-results", text, cap=_SEARCH_CAP) or text


async def _web_fetch_impl(url: str, max_chars: int = 30000) -> str:
    max_chars = max(500, min(int(max_chars or 30000), 60000))
    out = await _fetch.fetch_page(url, max_chars=max_chars)
    audit("web_fetch", url=url,
          outcome=("error" if "error" in out
                   else "redirect" if "redirect_to" in out else "ok"),
          final_url=out.get("final_url", ""))
    if "error" in out:
        return json.dumps({"error": out["error"]}, ensure_ascii=False)
    if "redirect_to" in out:
        # Unfenced on purpose: this is OUR instruction to the model, not
        # page content. Fencing it would tell the model to ignore it.
        return (f"That URL redirects to a different host: {out['redirect_to']}\n"
                f"Nothing was fetched. If that destination is what you want, "
                f"call web_fetch again with the new URL.")
    text = json.dumps(out, ensure_ascii=False)
    return fence_untrusted("web-page", text, cap=_PAGE_CAP) or text


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
