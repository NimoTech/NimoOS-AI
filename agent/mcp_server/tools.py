# NimoOS-AI/agent/mcp_server/tools.py
"""Adapter: re-expose 6 read-only skills as MCP tools. tools/call routes to
each skill's existing _impl; tools/list uses curated inputSchemas (so e.g.
read_document never exposes the Plan-2 path/ocr params). No capability logic
is reimplemented here."""
from __future__ import annotations

import asyncio
import json

from mcp_server import fs_gate
from skills.search import search as _search
from skills import wiki as _wiki
from skills import photos as _photos
from skills import notes as _notes
from wiki_client import WikiClient

MAX_TREE_NODES = 500
_SEARCH_MAX_TOP_K = 20


class McpToolError(Exception):
    """A tool-call business failure; the server maps it to isError=True."""


class ImageResult:
    """A tool result that must be returned as MCP ImageContent."""
    def __init__(self, data_b64: str, mime: str = "image/png"):
        self.data_b64 = data_b64
        self.mime = mime


def setup_user_context(user_id: str) -> None:
    """Set the per-user ContextVars the whitelisted skills read. MUST run in the
    same asyncio task that will dispatch the tool (ContextVars are per-task)."""
    _search.USER_ID_VAR.set(str(user_id))
    _wiki.WIKI_CLIENT_VAR.set(WikiClient(user_id=str(user_id)))
    _photos.USER_ID_VAR.set(str(user_id))
    _notes.USER_ID_VAR.set(str(user_id))


async def _h_search(args: dict) -> str:
    top_k = min(int(args.get("top_k", 5) or 5), _SEARCH_MAX_TOP_K)
    return await _search._nimoos_search_impl(
        args["query"], args.get("sources"), args.get("filters"), top_k)


async def _h_read_document(args: dict):
    fid = args.get("file_id")
    path = args.get("path")
    if fid and path:
        raise McpToolError("provide file_id or path, not both")
    if not fid and not path:
        raise McpToolError("provide file_id or path")
    max_chars = int(args.get("max_chars", 24000) or 24000)
    if fid:
        return await _search._read_document_impl(
            file_id=fid, path=None, ocr=False,
            offset=int(args.get("offset", 0) or 0), max_chars=max_chars)
    try:
        abs_path = fs_gate.mcp_resolve_read_path(path)
    except fs_gate.McpPathDenied as e:
        raise McpToolError(f"path not allowed: {e}")
    uid = _search.USER_ID_VAR.get() or None
    try:
        res = await _search._parser_client.extract(
            abs_path, ocr=bool(args.get("ocr", False)),
            max_chars=max_chars, user_id=uid)
    except Exception as e:
        raise McpToolError(f"extract failed: {e}")
    return json.dumps(res, ensure_ascii=False)


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


_PHOTOS_MAX_LIMIT = 50


async def _h_search_photos(args: dict) -> str:
    limit = min(int(args.get("limit", 20) or 20), _PHOTOS_MAX_LIMIT)
    return await _photos._search_photos_impl(
        args["query"], int(args.get("year", 0) or 0), limit,
        args.get("ocr_text", "") or "")


async def _h_list_albums(args: dict) -> str:
    return await _photos._list_albums_impl()


async def _h_view_document_page(args: dict):
    try:
        abs_path = fs_gate.mcp_resolve_read_path(args["path"])
    except fs_gate.McpPathDenied as e:
        raise McpToolError(f"path not allowed: {e}")
    uid = _search.USER_ID_VAR.get() or None
    page = int(args.get("page", 1) or 1)
    try:
        rendered = await asyncio.wait_for(
            _search._parser_client.render_pages(abs_path, page, page,
                                                scale=1.5, user_id=uid),
            timeout=60)
    except asyncio.TimeoutError:
        raise McpToolError("render timed out (document too complex)")
    except Exception as e:
        raise McpToolError(f"render failed: {e}")
    pages = rendered.get("pages") or []
    if not pages:
        raise McpToolError(f"page {page} not found; the document may have fewer pages")
    png_b64 = pages[0].get("png_b64")
    if not png_b64:
        raise McpToolError("render produced no image")
    return ImageResult(png_b64, "image/png")


async def _h_list_notes(args: dict) -> str:
    return await _notes._list_notes_impl(
        str(args.get("type") or ""), str(args.get("status") or ""),
        int(args.get("limit") or 20))


async def _h_read_note(args: dict) -> str:
    note_id = str(args.get("note_id") or "")
    if not note_id:
        raise McpToolError("note_id is required")
    return await _notes._read_note_impl(note_id)


_STR = {"type": "string"}
_INT = {"type": "integer"}

TOOL_SPECS = [
    {"name": "nimoos_search",
     "description": ("Search the user's NAS: semantic content, filenames, "
                     "photos, and notes. Returns grouped candidates with "
                     "file_id values you pass to read_document/read_file_chunk. "
                     "top_k max 20."),
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": _STR,
         "sources": {**_STR, "description": "comma list of semantic,filenames,images,notes"},
         "filters": {**_STR, "description": "JSON filter for the semantic source"},
         "top_k": {**_INT, "description": "hits per source, max 20"}}},
     "handler": _h_search},
    {"name": "read_document",
     "description": ("Read a document's extracted text. Provide EITHER file_id "
                     "(from nimoos_search, indexed) OR an absolute path under "
                     "/DATA (any file). Not both. Use offset/max_chars to page."),
     "inputSchema": {"type": "object", "properties": {
         "file_id": _STR,
         "path": {**_STR, "description": "absolute path under /DATA"},
         "ocr": {"type": "boolean", "description": "force OCR (path only)"},
         "offset": _INT,
         "max_chars": {**_INT, "description": "default 24000"}}},
     "handler": _h_read_document},
    {"name": "view_document_page",
     "description": ("Render a PDF page to an image and return it, so YOU (the "
                     "client model) can look at scanned pages, tables, charts, or "
                     "layout that text extraction misses. path must be an absolute "
                     "path under /DATA."),
     "inputSchema": {"type": "object", "required": ["path"], "properties": {
         "path": {**_STR, "description": "absolute path under /DATA (PDF)"},
         "page": {**_INT, "description": "1-based page number, default 1"}}},
     "handler": _h_view_document_page},
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
    {"name": "search_photos",
     "description": ("Search the user's photos by semantic description (CLIP). "
                     "`query` MUST be a short ENGLISH description (e.g. 'sunset "
                     "at beach'); non-English is rejected. `ocr_text` (optional) "
                     "is a keyword printed INSIDE the photo, in its own language, "
                     "for receipts/screenshots. limit max 50."),
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": _STR,
         "year": {**_INT, "description": "optional year filter, 0 = none"},
         "limit": {**_INT, "description": "max results, 1-50"},
         "ocr_text": {**_STR, "description": "optional in-photo text keyword"}}},
     "handler": _h_search_photos},
    {"name": "list_albums",
     "description": ("List the user's photo albums: {count, albums:[{id, name, "
                     "assetCount, dateStart, dateEnd}]} (capped at 100)."),
     "inputSchema": {"type": "object", "properties": {}},
     "handler": _h_list_albums},
    {
        "name": "list_notes",
        "description": "List knowledge notes from the NimoOS knowledge base "
                       "(title/description/tags/status metadata), newest first.",
        "inputSchema": {"type": "object", "properties": {
            "type": {"type": "string",
                     "description": "Filter by type: note|summary|insight|digest"},
            "status": {"type": "string",
                       "description": "Filter by status: draft|curated|archived"},
            "limit": {"type": "integer", "description": "Max results (1-100), default 20"},
        }},
        "handler": _h_list_notes,
    },
    {
        "name": "read_note",
        "description": "Read one knowledge note by id — metadata plus full Markdown body.",
        "inputSchema": {"type": "object", "properties": {
            "note_id": {"type": "string",
                        "description": "Note id from list_notes or nimoos_search"},
        }, "required": ["note_id"]},
        "handler": _h_read_note,
    },
]

_BY_NAME = {s["name"]: s for s in TOOL_SPECS}


def list_tool_defs() -> list[dict]:
    return [{"name": s["name"], "description": s["description"],
             "inputSchema": s["inputSchema"]} for s in TOOL_SPECS]


async def call(name: str, args: dict) -> str:
    spec = _BY_NAME[name]  # raises KeyError for non-whitelisted names
    return await spec["handler"](args or {})
