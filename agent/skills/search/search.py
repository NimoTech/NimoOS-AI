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
from parser_client import ParserClient
from skills import filesystem as _fsskill
from skills import photos as _photos
from fs import ops as _fsops, paths as _fspaths, ignore as _fsignore

_client = SearchClient()
_parser_client = ParserClient()

_FS_GATE_ERRORS = (
    _fspaths.PermissionDenied,
    _fsignore.BlockedImplicit,
    _fsignore.BlockedHardBlacklist,
    _fsignore.BlockedGitignore,
    LookupError,  # fs ContextVars unset (no active run context)
)

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


async def _read_document_impl(file_id: Optional[str] = None,
                              path: Optional[str] = None,
                              ocr: bool = False,
                              offset: int = 0,
                              max_chars: int = 24000) -> str:
    # file_id + not ocr → indexed fast path (M1, via Search).
    if file_id and not ocr:
        uid = USER_ID_VAR.get() or None
        try:
            result = await _client.invoke_tool("read_document", {
                "file_id": file_id, "offset": offset, "max_chars": max_chars,
            }, user_id=uid)
        except httpx.HTTPError as e:
            return json.dumps({"error": f"read_document failed: {e}"},
                              ensure_ascii=False)
        return json.dumps(result, ensure_ascii=False)

    # path (or forced ocr) → on-demand extraction via Parser, gated by the
    # same per-session filesystem authorization read_file uses.
    if not path:
        return json.dumps(
            {"error": "provide file_id (indexed) or path (any file)"},
            ensure_ascii=False)
    try:
        # Build ctx INSIDE the try: SESSION_ID_VAR/DB_VAR have no default, so an
        # unset run context raises LookupError here — which _FS_GATE_ERRORS
        # catches -> error JSON (never reaches Parser).
        ctx = {
            "session_id": _fsskill.SESSION_ID_VAR.get(),
            "conn": _fsskill.DB_VAR.get(),
            "user_patterns": _fsskill.USER_PATTERNS_VAR.get([]),
        }
        abs_path = _fsops._resolve_and_gate(ctx, path)
    except _FS_GATE_ERRORS as e:
        return json.dumps(
            {"error": f"not authorized to read that path: {e}"},
            ensure_ascii=False)
    uid = USER_ID_VAR.get() or None
    try:
        result = await _parser_client.extract(
            abs_path, ocr=ocr, max_chars=max_chars, user_id=uid)
    except Exception as e:
        return json.dumps({"error": f"document extraction failed: {e}"},
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
async def read_document(file_id: Optional[str] = None,
                        path: Optional[str] = None,
                        ocr: bool = False,
                        offset: int = 0,
                        max_chars: int = 24000) -> str:
    """Read a document's full text — PDF, Word, PowerPoint, Excel, HTML,
    Markdown, or plain text.

    Two ways to call it:
    - file_id (from nimoos_search) — fast, reads the already-indexed text with
      [Page N] markers; supports offset paging for long docs.
    - path — read any file by absolute path, including files NOT yet indexed
      (e.g. just uploaded). The text is extracted on demand. Set ocr=true for
      scanned/image PDFs. You may only read paths within your authorized scope
      (the same scope read_file uses).

    Args:
        file_id: Indexed file id from nimoos_search (preferred when available).
        path: Absolute path to read on demand (for unindexed files).
        ocr: Force OCR extraction (scanned PDFs); implies the path route.
        offset: Character offset for paging the indexed (file_id) route.
        max_chars: Maximum characters to return.
    """
    return await _read_document_impl(file_id, path, ocr, offset, max_chars)


async def _view_document_page_impl(path: str, page: int = 1,
                                   question: str = "") -> str:
    cfg = _photos.VISION_CFG_VAR.get()
    if not cfg.get("ok"):
        return json.dumps(
            {"error": "current model has no vision; use "
                      "read_document(path, ocr=true) for scanned text instead"},
            ensure_ascii=False)
    try:
        ctx = {
            "session_id": _fsskill.SESSION_ID_VAR.get(),
            "conn": _fsskill.DB_VAR.get(),
            "user_patterns": _fsskill.USER_PATTERNS_VAR.get([]),
        }
        abs_path = _fsops._resolve_and_gate(ctx, path)
    except _FS_GATE_ERRORS as e:
        return json.dumps(
            {"error": f"not authorized to read that path: {e}"},
            ensure_ascii=False)
    uid = USER_ID_VAR.get() or None
    try:
        rendered = await _parser_client.render_pages(
            abs_path, page, page, user_id=uid)
    except Exception as e:
        return json.dumps({"error": f"page render failed: {e}"},
                          ensure_ascii=False)
    pages = rendered.get("pages") or []
    if not pages:
        return json.dumps({"error": f"page {page} not found"}, ensure_ascii=False)
    prompt = question or (
        f"Describe page {page} of this document — its text, tables, figures, "
        f"and layout.")
    desc, err = await _photos.describe_image(pages[0]["png_b64"], prompt)
    if err:
        return json.dumps({"error": f"vision failed: {err}"}, ensure_ascii=False)
    return json.dumps({"page": page, "description": desc}, ensure_ascii=False)


@function_tool
async def view_document_page(path: str, page: int = 1, question: str = "") -> str:
    """Render a document PAGE to an image and look at it with the vision model.
    Use when read_document's text is not enough — scanned/image PDFs, complex
    tables, charts/diagrams, or "what does this page look like" questions.
    Requires a vision-capable model (otherwise use read_document(path, ocr=true)).
    PDF only. You may only view paths within your authorized scope.

    Args:
        path: Absolute path to the PDF.
        page: 1-based page number to render.
        question: Optional specific question about the page.
    """
    return await _view_document_page_impl(path, page, question)


SEARCH_TOOLS = [nimoos_search, read_file_chunk, read_document, view_document_page]
