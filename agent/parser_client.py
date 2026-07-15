"""HTTP client for the local NimoOS Parser service.

Mirrors search_client.SearchClient. Parser is an internal localhost service;
the agent runs in host-network mode and reads Parser's address from the
runtime discovery file (read-only mounted at /var/run/nimoos). Used by
read_document(path=...) for on-demand docling extraction of unindexed files.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

DEFAULT_DISCOVERY_PATH = os.environ.get(
    "PARSER_DISCOVERY_PATH", "/var/run/nimoos/parser.url")
# docling on a large PDF can take tens of seconds; keep this generous.
DEFAULT_TIMEOUT_S = 120.0


class ParserClient:
    def __init__(self, discovery_path: str = DEFAULT_DISCOVERY_PATH,
                 *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._discovery_path = discovery_path
        self._base_url: Optional[str] = os.environ.get("PARSER_BASE_URL")
        self._client = httpx.AsyncClient(timeout=timeout_s)

    def _resolve_base_url(self) -> str:
        if self._base_url:
            return self._base_url
        try:
            with open(self._discovery_path, "r") as f:
                self._base_url = f.read().strip()
        except OSError as e:
            raise RuntimeError(f"parser address unavailable ({self._discovery_path}): {e}")
        if not self._base_url:
            raise RuntimeError(f"parser address file empty: {self._discovery_path}")
        return self._base_url

    async def extract(self, path: str, ocr: bool = False,
                      max_chars: int = 24000,
                      user_id: Optional[str] = None) -> dict[str, Any]:
        base = self._resolve_base_url()
        headers: dict[str, str] = {}
        if user_id:
            headers["X-NimoOS-User-ID"] = user_id
        r = await self._client.post(
            f"{base}/v1/parser/extract",
            json={"path": path, "ocr": ocr, "max_chars": max_chars},
            headers=headers,
        )
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            body = ""
            try:
                body = (r.text or "").strip()
            except Exception:
                pass
            msg = f"{e}: {body}" if body else str(e)
            raise httpx.HTTPStatusError(msg, request=e.request, response=e.response) from e
        return r.json()

    async def render_pages(self, path: str, page_start: int, page_end: int,
                           scale: float = 2.0,
                           user_id: Optional[str] = None) -> dict[str, Any]:
        base = self._resolve_base_url()
        headers: dict[str, str] = {}
        if user_id:
            headers["X-NimoOS-User-ID"] = user_id
        r = await self._client.post(
            f"{base}/v1/parser/render/pages",
            json={"path": path, "page_start": page_start,
                  "page_end": page_end, "scale": scale},
            headers=headers,
        )
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            body = (r.text or "").strip()
            msg = f"{e}: {body}" if body else str(e)
            raise httpx.HTTPStatusError(msg, request=e.request, response=e.response) from e
        return r.json()

    async def agent_memory_upsert(self, user_id, session_id, chunks):
        base = self._resolve_base_url()
        r = await self._client.post(
            f"{base}/v1/parser/agent-memory/upsert",
            json={"user_id": str(user_id), "session_id": session_id,
                  "chunks": chunks})
        r.raise_for_status()
        return r.json()

    async def agent_memory_query(self, user_id, query, top_k=5):
        base = self._resolve_base_url()
        r = await self._client.post(
            f"{base}/v1/parser/agent-memory/query",
            json={"user_id": str(user_id), "query": query, "top_k": top_k})
        r.raise_for_status()
        return r.json()

    async def agent_memory_delete(self, user_id, session_id):
        base = self._resolve_base_url()
        r = await self._client.post(
            f"{base}/v1/parser/agent-memory/delete",
            json={"user_id": str(user_id), "session_id": session_id})
        r.raise_for_status()
        return r.json()

    async def aclose(self) -> None:
        await self._client.aclose()
