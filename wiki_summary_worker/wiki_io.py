"""HTTP client wrapper for the three Wiki internal endpoints.

Errors are NOT wrapped here — httpx.HTTPError leaks through deliberately.
That signal means "wiki is unhealthy, worker should break the round and
retry next timer fire", not "this particular node failed". Wrapping it
would defeat the per-node-vs-transient classification in worker.run_once.
"""
from __future__ import annotations
from typing import Any
import httpx

from wiki_summary_worker import discovery


def _make_client() -> httpx.Client:
    return httpx.Client(timeout=30)


def fetch_needs_summary(limit: int) -> list[dict[str, Any]]:
    url = discovery.wiki_url() + "/v1/wiki/_internal/needs-summary"
    with _make_client() as c:
        r = c.get(url, params={"limit": limit})
        r.raise_for_status()
        return r.json().get("nodes", []) or []


def fetch_node_evidence(*, path: str, text_limit: int, pdf_limit: int) -> dict[str, Any]:
    url = discovery.wiki_url() + "/v1/wiki/_internal/node-evidence"
    with _make_client() as c:
        r = c.get(url, params={"path": path, "text_limit": text_limit, "pdf_limit": pdf_limit})
        r.raise_for_status()
        return r.json()


def post_summary(
    *, path: str, ai_label: str, summary: str,
    based_on_last_modified_ms: int, generator_version: str,
) -> None:
    url = discovery.wiki_url() + "/v1/wiki/_internal/summary"
    body = {
        "path": path,
        "ai_label": ai_label,
        "summary": summary,
        "based_on_last_modified_ms": based_on_last_modified_ms,
        "generator_version": generator_version,
    }
    with _make_client() as c:
        r = c.post(url, json=body)
        r.raise_for_status()
