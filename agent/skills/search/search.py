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


async def _nimoos_search_impl(query: str, sources: Optional[str] = None,
                              filters: Optional[str] = None, top_k: int = 5) -> str:
    args: dict = {"query": query, "top_k": top_k}
    if sources is not None:
        # accept "images" / "images,filenames" / JSON '["images"]'
        s = sources.strip()
        if s.startswith("["):
            try:
                args["sources"] = json.loads(s)
            except json.JSONDecodeError as e:
                return json.dumps({"error": f"invalid sources JSON: {e}"}, ensure_ascii=False)
        else:
            args["sources"] = [p.strip() for p in s.split(",") if p.strip()]
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


async def _read_document_impl(file_id: str, offset: int = 0,
                              max_chars: int = 24000) -> str:
    uid = USER_ID_VAR.get() or None
    try:
        result = await _client.invoke_tool("read_document", {
            "file_id": file_id, "offset": offset, "max_chars": max_chars,
        }, user_id=uid)
    except httpx.HTTPError as e:
        return json.dumps({"error": f"read_document failed: {e}"},
                          ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


@function_tool
async def nimoos_search(query: str, sources: Optional[str] = None,
                        filters: Optional[str] = None, top_k: int = 5) -> str:
    """Unified search over the user's NAS: by content (semantic), by filename,
    and photos. Returns grouped candidates {semantic, filenames, images} for the
    user to choose from.

    Args:
        query: The search query string.
        sources: Optional comma-separated subset of "semantic", "filenames",
            "images" (e.g. "images" to search photos only). Omit to search all.
        filters: Optional JSON-encoded filter object (applies to the semantic
            source only): root_ids, mime_prefix, kind_in, lang_in, mtime_after_ms.
        top_k: Max hits per source (default 5, max 20).
    """
    return await _nimoos_search_impl(query, sources, filters, top_k)


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


@function_tool
async def read_document(file_id: str, offset: int = 0,
                        max_chars: int = 24000) -> str:
    """Read a document's full extracted text by file_id, reconstructed from the
    search index with [Page N] markers. Call this after nimoos_search returns a
    file_id when you need the whole document rather than a short preview.

    If the result has "truncated": true the document is long — page with
    "offset" set to the returned "next_offset", or (better for finding one
    specific fact) use nimoos_search to locate the relevant passage instead of
    reading the whole document.

    Args:
        file_id: File identifier returned by nimoos_search.
        offset: Character offset to start from (default 0; for paging).
        max_chars: Maximum characters to return (default 24000).
    """
    return await _read_document_impl(file_id, offset, max_chars)


SEARCH_TOOLS = [nimoos_search, read_file_chunk, read_document]
