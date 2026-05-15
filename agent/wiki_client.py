"""HTTP wrapper for nimoos-wiki.

One instance per Agent session; cache lives on the instance and is cleared at
the top of every AgentRunner.run call via reset_cache(). All requests go
through the gateway (localhost:80) so wiki port churn on restart is invisible
to us. Wiki's middleware grants a localhost JWT exemption for in-host services
(agent, nimoos-cli) and honors the X-NimoOS-User-ID header we attach.
"""
from __future__ import annotations

import os
from typing import Optional

import httpx


DEFAULT_BASE_URL = os.environ.get("WIKI_BASE_URL", "http://127.0.0.1")
DEFAULT_TIMEOUT = 5.0


class WikiClient:
    def __init__(self, *, user_id: str = "",
                 base_url: str = DEFAULT_BASE_URL,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base = base_url.rstrip("/")
        self.user_id = user_id
        self._timeout = timeout
        self._http: httpx.AsyncClient | None = None
        self._node_cache: dict[str, dict] = {}
        self._tree_cache: dict[str, list[dict]] = {}  # keyed by root_id ("" for all)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=self.base, timeout=self._timeout)
        return self._http

    def _headers(self) -> dict[str, str]:
        h = {}
        if self.user_id:
            h["X-NimoOS-User-ID"] = self.user_id
        return h

    def reset_cache(self) -> None:
        self._node_cache.clear()
        self._tree_cache.clear()

    def invalidate_node(self, path: str) -> None:
        self._node_cache.pop(path, None)
        # tree carries user_notes_updated_at which just changed
        self._tree_cache.clear()

    async def get_node(self, path: str) -> Optional[dict]:
        if path in self._node_cache:
            return self._node_cache[path]
        r = await self._client().get("/v1/wiki/node",
                                     params={"path": path},
                                     headers=self._headers())
        if r.status_code == 404:
            return None
        r.raise_for_status()
        node = r.json()
        self._node_cache[path] = node
        return node

    async def list_full_tree(self, root_id: str = "") -> list[dict]:
        if root_id in self._tree_cache:
            return self._tree_cache[root_id]
        params = {}
        if root_id:
            params["root_id"] = root_id
        r = await self._client().get("/v1/wiki/tree",
                                     params=params,
                                     headers=self._headers())
        r.raise_for_status()
        tree = r.json()
        self._tree_cache[root_id] = tree
        return tree

    async def recent_changes(self, root_id: str, since_ms: int = 0,
                             limit: int = 50) -> list[dict]:
        params = {"root_id": root_id, "since_ms": str(since_ms), "limit": str(limit)}
        r = await self._client().get("/v1/wiki/recent-changes",
                                     params=params,
                                     headers=self._headers())
        r.raise_for_status()
        return r.json()

    async def put_user_notes(self, path: str, body: str,
                             if_match: Optional[str]) -> dict:
        headers = self._headers()
        if if_match:
            headers["If-Match"] = if_match
        r = await self._client().put("/v1/wiki/user-notes",
                                     params={"path": path},
                                     content=body.encode("utf-8"),
                                     headers=headers)
        r.raise_for_status()
        return r.json()

    async def post_root(self, path: str, level: str) -> dict:
        r = await self._client().post("/v1/wiki/roots",
                                      json={"path": path, "level": level},
                                      headers=self._headers())
        r.raise_for_status()
        return r.json()

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
