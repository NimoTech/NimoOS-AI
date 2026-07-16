"""Memory tools exposed to the Agent. Mirrors the static-tool + _impl pattern
used by skills/search and skills/wiki — tool definitions live in Python.
Identity is injected per-run via USER_ID_VAR (set by AgentRunner.run); user_id
is NOT a tool parameter, so the model cannot name or forge another user's id.
"""
from __future__ import annotations

import json
from contextvars import ContextVar

from agents import function_tool

import db as db_module
import memory_store
from fences import fence_untrusted
from memory_lock import get_user_lock

# Set per-run by AgentRunner.run; read at tool-call time.
USER_ID_VAR: ContextVar[str] = ContextVar("memory_user_id", default="")
SESSION_ID_VAR: ContextVar[str] = ContextVar("memory_session_id", default="")


async def _remember_impl(text: str, kind: str = "fact", priority: int = 0) -> str:
    uid = USER_ID_VAR.get()
    if not uid:
        return json.dumps({"error": "no user context"}, ensure_ascii=False)
    if kind not in memory_store.VALID_KINDS:
        return json.dumps(
            {"error": f"invalid kind: {kind}; use one of {memory_store.VALID_KINDS}"},
            ensure_ascii=False)
    conn = db_module.get_connection()
    if not memory_store.is_memory_enabled(conn, uid):
        return json.dumps({"status": "disabled"}, ensure_ascii=False)
    async with get_user_lock(uid):
        dup = memory_store.find_active_duplicate(conn, uid, text)
        if dup:
            return json.dumps({"status": "duplicate", "id": dup}, ensure_ascii=False)
        sid = SESSION_ID_VAR.get() or None
        # Mirror the extraction low-trust gate: memory recorded via the
        # explicit remember() tool while serving a channel-sourced session
        # (Telegram/Discord/...) may have been shaped by untrusted external
        # content the user relayed in. Mark it low-trust so it is stored and
        # visible in the UI but never re-injected into future system prompts.
        _srow = conn.execute(
            "SELECT source FROM sessions WHERE id=?", (sid,)).fetchone() if sid else None
        _trust = "low" if (_srow and _srow["source"] and _srow["source"] != "web") else "normal"
        mem_id = memory_store.add_memory(
            conn, uid, text, kind, source="tool", trust=_trust,
            priority=priority, origin_session_id=sid)
    return json.dumps({"status": "added", "id": mem_id}, ensure_ascii=False)


async def _forget_impl(query_or_id: str) -> str:
    uid = USER_ID_VAR.get()
    if not uid:
        return json.dumps({"error": "no user context"}, ensure_ascii=False)
    conn = db_module.get_connection()
    row = conn.execute(
        "SELECT id FROM memory_entries "
        "WHERE id=? AND user_id=? AND status='active'",
        (query_or_id, uid)).fetchone()
    if row:
        memory_store.disable_memory(conn, row["id"])
        return json.dumps({"status": "forgotten", "ids": [row["id"]]},
                          ensure_ascii=False)
    ids = memory_store.disable_by_text(conn, uid, query_or_id)
    if not ids:
        return json.dumps({"status": "not_found", "ids": []}, ensure_ascii=False)
    return json.dumps({"status": "forgotten", "ids": ids}, ensure_ascii=False)


_recall_parser_client = None


def _query_agent_memory(user_id, query, top_k=5):
    """Seam over ParserClient.agent_memory_query (monkeypatched in tests)."""
    global _recall_parser_client
    if _recall_parser_client is None:
        from parser_client import ParserClient
        _recall_parser_client = ParserClient()
    return _recall_parser_client.agent_memory_query(user_id, query, top_k=top_k)


async def _recall_impl(query: str, top_k: int = 5) -> str:
    uid = USER_ID_VAR.get()
    if not uid:
        return json.dumps({"error": "no user context"}, ensure_ascii=False)
    conn = db_module.get_connection()
    if not memory_store.is_memory_enabled(conn, uid):
        return json.dumps({"status": "disabled"}, ensure_ascii=False)
    try:
        res = await _query_agent_memory(uid, query, top_k=top_k)
    except Exception:
        return json.dumps({"status": "unavailable"}, ensure_ascii=False)
    # Episodic hits re-surface content from PAST conversations that may have
    # carried injected instructions (esp. channel sessions). Fence them as
    # untrusted data before handing back to the model. fence_untrusted returns
    # "" for empty/whitespace payloads, so fall back to the raw JSON to
    # preserve the no-hits UX.
    payload = json.dumps({"hits": res.get("hits", [])}, ensure_ascii=False)
    return fence_untrusted("recall", payload, cap=60000) or payload


@function_tool
async def recall(query: str, top_k: int = 5) -> str:
    """Recall relevant snippets from your PAST conversations with this user
    (cross-session episodic memory). Use when the user refers to something
    discussed before ("what did we decide about…", "the issue from last time").
    Returns matching conversation snippets; empty if nothing relevant."""
    return await _recall_impl(query, top_k)


@function_tool
async def remember(text: str, kind: str = "fact", priority: int = 0) -> str:
    """Persist a durable fact, preference, or long-term goal about the user so
    future conversations remember it. Use only for stable, user-specific facts
    the user stated or clearly implied — not transient task details.
    kind must be one of: preference, fact, goal. priority: higher = injected first."""
    return await _remember_impl(text, kind, priority)


@function_tool
async def forget(query_or_id: str) -> str:
    """Disable a stored user memory, by its id or by matching text. The memory
    is soft-disabled (kept for audit), not physically deleted."""
    return await _forget_impl(query_or_id)


MEMORY_TOOLS = [remember, forget, recall]
