"""HTTP wrapper for nimoos-search. All requests go through the gateway
(localhost:80) so search port churn on restart is invisible to us. Search's
middleware grants a localhost JWT exemption for in-host services (agent,
nimoos-cli) and honors the X-NimoOS-User-ID header we attach.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx


DEFAULT_BASE_URL = os.environ.get("SEARCH_BASE_URL", "http://127.0.0.1")
DEFAULT_TIMEOUT = 10.0


class SearchClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL,
                 *, timeout_s: float = DEFAULT_TIMEOUT) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def invoke_tool(self, name: str, arguments: dict,
                          user_id: Optional[str] = None) -> dict[str, Any]:
        headers = {}
        if user_id:
            headers["X-NimoOS-User-ID"] = user_id
        r = await self._client.post(
            f"{self._base_url}/v1/search/agent/tool",
            json={"name": name, "arguments": arguments},
            headers=headers,
        )
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            # raise_for_status()'s default message names only the status and URL,
            # dropping the response body — which is where search puts the real
            # cause (e.g. "parser embed 500: ..."). Re-raise the same error type
            # (callers catch httpx.HTTPError) with the body appended.
            body = (r.text or "").strip()
            msg = f"{e}: {body}" if body else str(e)
            raise httpx.HTTPStatusError(
                msg, request=e.request, response=e.response) from e
        return r.json()

    async def search_text(self, query: str, *, user_id: str, top_k: int = 10,
                          rerank: bool = True, timeout_s: float | None = None) -> dict[str, Any]:
        """POST /v1/search/text — semantic hits with FULL chunk text (the agent
        tool path trims previews to 200 chars; the ask pipeline needs the text).
        Search's own per-user root scoping applies via X-NimoOS-User-ID."""
        r = await self._client.post(
            f"{self._base_url}/v1/search/text",
            json={"query": query, "top_k": top_k, "rerank": rerank},
            headers={"X-NimoOS-User-ID": str(user_id)},
            timeout=timeout_s if timeout_s is not None else httpx.USE_CLIENT_DEFAULT,
        )
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            body = (r.text or "").strip()
            raise httpx.HTTPStatusError(
                f"{e}: {body}" if body else str(e), request=e.request, response=e.response) from e
        return r.json()

    async def aclose(self) -> None:
        await self._client.aclose()
