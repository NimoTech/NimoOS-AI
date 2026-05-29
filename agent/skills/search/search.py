"""Search tools exposed to the Agent. Mirrors the static-tool + _impl pattern
used by skills/wiki — tool definitions live in Python, not fetched from the
server. Identity is injected per-run via USER_ID_VAR (set by AgentRunner.run),
never passed by the LLM: user_id is not a tool parameter, so the model cannot
name or forge another user's id. The Go search service is the real enforcement
boundary (it derives accessible roots from the X-NimoOS-User-ID header).
"""
from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Optional

import httpx
from agents import function_tool

# search_client.py is a top-level module in agent/; agent/ is on sys.path
from search_client import SearchClient

_client = SearchClient()

# Set per-run by AgentRunner.run; read at tool-call time.
USER_ID_VAR: ContextVar[str] = ContextVar("search_user_id", default="")


async def _nimoos_search_impl(query: str, modality: str = "auto",
                              filters: Optional[str] = None, top_k: int = 5) -> str:
    args: dict = {"query": query, "modality": modality, "top_k": top_k}
    if filters is not None:
        try:
            args["filters"] = json.loads(filters)
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"invalid filters JSON: {e}"},
                              ensure_ascii=False)
    uid = USER_ID_VAR.get() or None
    try:
        result = await _client.invoke_tool("nimoos_search", args, user_id=uid)
    except httpx.HTTPError as e:
        return json.dumps({"error": f"search request failed: {e}"},
                          ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


async def _read_file_chunk_impl(file_id: str, kind: str, chunk_no: int,
                                window: int = 2) -> str:
    uid = USER_ID_VAR.get() or None
    try:
        result = await _client.invoke_tool("read_file_chunk", {
            "file_id": file_id, "kind": kind,
            "chunk_no": chunk_no, "window": window,
        }, user_id=uid)
    except httpx.HTTPError as e:
        return json.dumps({"error": f"read_file_chunk failed: {e}"},
                          ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


@function_tool
async def nimoos_search(query: str, modality: str = "auto",
                        filters: Optional[str] = None, top_k: int = 5) -> str:
    """Search the user's personal NAS for relevant content. Use this when the
    user asks about files, documents, or any past content stored on their NAS.
    Returns up to top_k hits with previews and file paths.

    Args:
        query: The search query string.
        modality: Search modality — "auto" (default) or "text". MVP is text-only.
        filters: Optional JSON-encoded filter object. Supported fields:
            root_ids (string[]), mime_prefix (string[], e.g. ["text/markdown"]),
            kind_in (string[]: body|ocr|caption|transcript|summary; MVP: body),
            lang_in (string[], e.g. ["zh","en"]), mtime_after_ms (int, unix ms).
            Example: '{"mime_prefix":["text/markdown"],"mtime_after_ms":1714500000000}'.
        top_k: Maximum number of hits to return (default 5, max 20).
    """
    return await _nimoos_search_impl(query, modality, filters, top_k)


@function_tool
async def read_file_chunk(file_id: str, kind: str, chunk_no: int,
                          window: int = 2) -> str:
    """Fetch the chunk at (file_id, kind, chunk_no) plus a small window of
    neighboring chunks. Call this after nimoos_search returns a hit when the
    preview is too short to answer the user's question.

    Args:
        file_id: File identifier returned by nimoos_search.
        kind: Chunk kind — one of body, ocr, caption, transcript, summary
            (MVP only produces "body").
        chunk_no: Zero-based chunk index.
        window: Neighboring chunks to include on each side (default 2, max 5).
    """
    return await _read_file_chunk_impl(file_id, kind, chunk_no, window)


SEARCH_TOOLS = [nimoos_search, read_file_chunk]
