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
        r.raise_for_status()
        return r.json()

    async def aclose(self) -> None:
        await self._client.aclose()
