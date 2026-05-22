"""Search tools exposed to the Agent. Mirrors the static-tool pattern used by
skills/wiki — tool definitions live in Python, not fetched from the server.
"""
from __future__ import annotations

from typing import Optional

from agents import function_tool

# search_client.py is a top-level module in agent/; agent/ is on sys.path
from search_client import SearchClient

_client = SearchClient()


@function_tool
async def nimoos_search(query: str, modality: str = "auto",
                        filters: Optional[str] = None, top_k: int = 5) -> str:
    """Search the user's personal NAS for relevant content. Use this when the
    user asks about files, photos, videos, documents, or any past content
    stored on their NAS. Returns up to top_k hits with previews and file paths.

    Args:
        query: The search query string.
        modality: Search modality — "text", "image", or "auto" (default).
        filters: Optional JSON-encoded filter object, e.g. '{"dir": "/DATA/Photos"}'.
        top_k: Maximum number of hits to return (default 5).
    """
    import json
    args: dict = {"query": query, "modality": modality, "top_k": top_k}
    if filters is not None:
        args["filters"] = json.loads(filters)
    result = await _client.invoke_tool("nimoos_search", args)
    return json.dumps(result, ensure_ascii=False)


@function_tool
async def read_file_chunk(file_id: str, kind: str, chunk_no: int,
                          window: int = 2) -> str:
    """Fetch the chunk at (file_id, kind, chunk_no) plus a small window of
    neighboring chunks. Call this after nimoos_search returns a hit when the
    preview is too short to answer the user's question.

    Args:
        file_id: File identifier returned by nimoos_search.
        kind: Chunk kind, e.g. "text" or "image".
        chunk_no: Zero-based chunk index.
        window: Number of neighboring chunks to include on each side (default 2).
    """
    import json
    result = await _client.invoke_tool("read_file_chunk", {
        "file_id": file_id, "kind": kind, "chunk_no": chunk_no, "window": window,
    })
    return json.dumps(result, ensure_ascii=False)


SEARCH_TOOLS = [nimoos_search, read_file_chunk]
