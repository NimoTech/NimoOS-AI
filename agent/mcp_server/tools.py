# NimoOS-AI/agent/mcp_server/tools.py
"""Adapter: re-expose 6 read-only skills as MCP tools. tools/call routes to
each skill's existing _impl; tools/list uses curated inputSchemas (so e.g.
read_document never exposes the Plan-2 path/ocr params). No capability logic
is reimplemented here."""
from __future__ import annotations

import json

from skills.search import search as _search
from skills import wiki as _wiki
from wiki_client import WikiClient

MAX_TREE_NODES = 500
_SEARCH_MAX_TOP_K = 20


def setup_user_context(user_id: str) -> None:
    """Set the per-user ContextVars the whitelisted skills read. MUST run in the
    same asyncio task that will dispatch the tool (ContextVars are per-task)."""
    _search.USER_ID_VAR.set(str(user_id))
    _wiki.WIKI_CLIENT_VAR.set(WikiClient(user_id=str(user_id)))


async def _h_search(args: dict) -> str:
    top_k = min(int(args.get("top_k", 5) or 5), _SEARCH_MAX_TOP_K)
    return await _search._nimoos_search_impl(
        args["query"], args.get("sources"), args.get("filters"), top_k)


async def _h_read_document(args: dict) -> str:
    return await _search._read_document_impl(
        file_id=args["file_id"], path=None, ocr=False,
        offset=int(args.get("offset", 0) or 0),
        max_chars=int(args.get("max_chars", 24000) or 24000))


async def _h_read_file_chunk(args: dict) -> str:
    return await _search._read_file_chunk_impl(
        args["file_id"], args["kind"], int(args["chunk_no"]),
        int(args.get("window", 2) or 2))


async def _h_wiki_get_node(args: dict) -> str:
    return await _wiki._wiki_get_node_impl(args["path"])


async def _h_wiki_list_full_tree(args: dict) -> str:
    raw = await _wiki._wiki_list_full_tree_impl(args.get("root_id", ""))
    try:
        nodes = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw  # error JSON from the skill — pass through
    if isinstance(nodes, dict) and "error" in nodes:
        return raw  # backend error — pass through unchanged
    if isinstance(nodes, list) and len(nodes) > MAX_TREE_NODES:
        return json.dumps({"truncated": True, "total": len(nodes),
                           "nodes": nodes[:MAX_TREE_NODES]}, ensure_ascii=False)
    return json.dumps({"truncated": False, "nodes": nodes}, ensure_ascii=False)


async def _h_wiki_recent_changes(args: dict) -> str:
    return await _wiki._wiki_recent_changes_impl(
        args["path"], int(args.get("since_days", 7) or 7),
        int(args.get("limit", 50) or 50))


_STR = {"type": "string"}
_INT = {"type": "integer"}

TOOL_SPECS = [
    {"name": "nimoos_search",
     "description": ("Search the user's NAS: semantic content, filenames, and "
                     "photos. Returns grouped candidates with file_id values you "
                     "pass to read_document/read_file_chunk. top_k max 20."),
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": _STR,
         "sources": {**_STR, "description": "comma list of semantic,filenames,images"},
         "filters": {**_STR, "description": "JSON filter for the semantic source"},
         "top_k": {**_INT, "description": "hits per source, max 20"}}},
     "handler": _h_search},
    {"name": "read_document",
     "description": ("Read an indexed document's extracted text by file_id "
                     "(from nimoos_search). Use offset/max_chars to page through "
                     "long documents."),
     "inputSchema": {"type": "object", "required": ["file_id"], "properties": {
         "file_id": _STR, "offset": _INT,
         "max_chars": {**_INT, "description": "default 24000"}}},
     "handler": _h_read_document},
    {"name": "read_file_chunk",
     "description": ("Read one indexed chunk of a file by file_id. kind/chunk_no "
                     "come from a nimoos_search hit; window fetches neighbours."),
     "inputSchema": {"type": "object",
                     "required": ["file_id", "kind", "chunk_no"], "properties": {
         "file_id": _STR, "kind": _STR, "chunk_no": _INT, "window": _INT}},
     "handler": _h_read_file_chunk},
    {"name": "wiki_get_node",
     "description": ("Read a Wiki node: child map, recent changes, notes, label. "
                     "path is absolute, e.g. /DATA/Projects/nimoos."),
     "inputSchema": {"type": "object", "required": ["path"],
                     "properties": {"path": _STR}},
     "handler": _h_wiki_get_node},
    {"name": "wiki_list_full_tree",
     "description": ("Full skeleton tree for a Wiki root (or all roots if root_id "
                     f"empty). Truncated to {MAX_TREE_NODES} nodes; check the "
                     "'truncated' flag."),
     "inputSchema": {"type": "object",
                     "properties": {"root_id": _STR}},
     "handler": _h_wiki_list_full_tree},
    {"name": "wiki_recent_changes",
     "description": "Recent file-system events under the Wiki root containing path.",
     "inputSchema": {"type": "object", "required": ["path"], "properties": {
         "path": _STR, "since_days": _INT, "limit": _INT}},
     "handler": _h_wiki_recent_changes},
]

_BY_NAME = {s["name"]: s for s in TOOL_SPECS}


def list_tool_defs() -> list[dict]:
    return [{"name": s["name"], "description": s["description"],
             "inputSchema": s["inputSchema"]} for s in TOOL_SPECS]


async def call(name: str, args: dict) -> str:
    spec = _BY_NAME[name]  # raises KeyError for non-whitelisted names
    return await spec["handler"](args or {})
