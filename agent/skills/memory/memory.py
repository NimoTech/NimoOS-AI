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
    dup = memory_store.find_active_duplicate(conn, uid, text)
    if dup:
        return json.dumps({"status": "duplicate", "id": dup}, ensure_ascii=False)
    sid = SESSION_ID_VAR.get() or None
    mem_id = memory_store.add_memory(
        conn, uid, text, kind, source="tool", priority=priority,
        origin_session_id=sid)
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


MEMORY_TOOLS = [remember, forget]
