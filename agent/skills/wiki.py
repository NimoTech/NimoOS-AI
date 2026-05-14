"""Wiki tools for the Agent loop.

Three read tools (get_node / list_full_tree / recent_changes) and three write
tools (append_user_notes / replace_user_notes / register_root — Task 7).

Every tool gates the target path against USER_PATTERNS_VAR (the user's
hard_blacklist) before touching the client. WIKI_CLIENT_VAR set to None
means wiki is unavailable; tools return a structured error.
"""
from __future__ import annotations

import json
import time
from contextvars import ContextVar
from typing import Optional

import pathspec
from agents import function_tool

from wiki_client import WikiClient


WIKI_CLIENT_VAR: ContextVar[Optional[WikiClient]] = ContextVar(
    "wiki_client", default=None)
CONFIRM_MGR_VAR: ContextVar = ContextVar("wiki_confirm_mgr", default=None)
SESSION_ID_VAR: ContextVar[str] = ContextVar("wiki_session_id", default="")
EVENT_QUEUE_VAR: ContextVar = ContextVar("wiki_event_queue", default=None)
USER_PATTERNS_VAR: ContextVar[list] = ContextVar(
    "wiki_user_patterns", default=[])


def _gate(path: str) -> Optional[str]:
    """Return error-JSON if path is on the user's hard_blacklist, else None.

    Applied at the entry of EVERY wiki tool — defense in depth. One pathspec
    match per call; matches the semantics of fs.ignore.gate.
    """
    patterns = USER_PATTERNS_VAR.get()
    if not patterns:
        return None
    spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)
    if spec.match_file(path.lstrip("/")):
        return json.dumps({
            "error": "path is on user's hard blacklist",
            "path": path,
        }, ensure_ascii=False)
    return None


async def _wiki_get_node_impl(path: str) -> str:
    if blocked := _gate(path):
        return blocked
    client = WIKI_CLIENT_VAR.get()
    if client is None:
        return json.dumps({"error": "wiki service unavailable"})
    try:
        node = await client.get_node(path)
    except Exception as e:
        return json.dumps({"error": f"wiki request failed: {e}"})
    if node is None:
        return json.dumps({"error": "node not found", "path": path})
    return json.dumps(node, ensure_ascii=False)


async def _wiki_list_full_tree_impl(root_id: str = "") -> str:
    client = WIKI_CLIENT_VAR.get()
    if client is None:
        return json.dumps({"error": "wiki service unavailable"})
    try:
        tree = await client.list_full_tree(root_id=root_id)
    except Exception as e:
        return json.dumps({"error": f"wiki request failed: {e}"})
    return json.dumps(tree, ensure_ascii=False)


async def _wiki_recent_changes_impl(path: str, since_days: int = 7,
                                    limit: int = 50) -> str:
    if blocked := _gate(path):
        return blocked
    client = WIKI_CLIENT_VAR.get()
    if client is None:
        return json.dumps({"error": "wiki service unavailable"})
    # Resolve path → nearest space ancestor → root_id is its path.
    try:
        tree = await client.list_full_tree()
    except Exception as e:
        return json.dumps({"error": f"wiki request failed: {e}"})
    root_path = ""
    for n in tree:
        if n.get("level") == "space" and path.startswith(n["path"]):
            if len(n["path"]) > len(root_path):
                root_path = n["path"]
    if not root_path:
        # Fallback: best-effort use the path itself; server may return empty
        root_path = path
    since_ms = int((time.time() * 1000) - since_days * 86400 * 1000)
    if since_ms < 0:
        since_ms = 0
    try:
        events = await client.recent_changes(root_id=root_path,
                                             since_ms=since_ms, limit=limit)
    except Exception as e:
        return json.dumps({"error": f"wiki request failed: {e}"})
    return json.dumps(events, ensure_ascii=False)


@function_tool
async def wiki_get_node(path: str) -> str:
    """Read a Wiki node's full record: child map, recent changes, user notes,
    AI label, ETag. Use this when the user asks about contents of a project or
    you need to confirm a path exists.

    Args:
        path: Absolute path, e.g. /DATA/Projects/nimoos
    """
    return await _wiki_get_node_impl(path)


@function_tool
async def wiki_list_full_tree(root_id: str = "") -> str:
    """Return the full skeleton tree (path/level/ai_label/timestamps) for a
    Wiki root, or for ALL roots if root_id is empty. Use when the system
    prompt's map is capped and you need more breadth.

    Args:
        root_id: A Wiki root id; empty string lists every root.
    """
    return await _wiki_list_full_tree_impl(root_id)


@function_tool
async def wiki_recent_changes(path: str, since_days: int = 7,
                              limit: int = 50) -> str:
    """List file-system events under a Wiki root recently.

    Args:
        path: Any path under the target root; the tool resolves to its root id.
        since_days: Look back this many days (default 7).
        limit: Max events to return (1..200, default 50).
    """
    return await _wiki_recent_changes_impl(path, since_days, limit)
