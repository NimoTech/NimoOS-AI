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

import httpx
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


# ---------------------------------------------------------------------------
# Write tools (Task 7)
# ---------------------------------------------------------------------------

TEXT_PREVIEW_CAP = 200


def _preview(text: str) -> str:
    if len(text) <= TEXT_PREVIEW_CAP:
        return text
    return f"{text[:TEXT_PREVIEW_CAP]}…(+{len(text) - TEXT_PREVIEW_CAP} more chars)"


async def _request_confirm(action: str, description: str, command: str) -> bool:
    """Register a ConfirmRequest, emit the SSE event, await the resolution."""
    mgr = CONFIRM_MGR_VAR.get()
    sink = EVENT_QUEUE_VAR.get()
    session_id = SESSION_ID_VAR.get()
    if mgr is None or sink is None or not session_id:
        # Misconfigured runtime — refuse write rather than silently bypass.
        return False
    confirm_id = mgr.register(session_id, action, description, command)
    await sink.put({
        "type": "confirmation_required",
        "confirm_id": confirm_id,
        "action": action,
        "description": description,
        "command": command,
    })
    return await mgr.wait(confirm_id)


async def _wiki_append_user_notes_impl(path: str, text: str) -> str:
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

    description = f"Append note to Wiki: {path}"
    command = (f"PUT /v1/wiki/user-notes path={path}\n"
               f"+ {_preview(text)}")
    if not await _request_confirm("wiki_append_notes", description, command):
        return json.dumps({"error": "user declined"})

    prior = node.get("user_notes", "")
    if prior:
        new_body = prior.rstrip() + "\n\n" + text + "\n"
    else:
        new_body = text + "\n"

    etag = node.get("etag")
    try:
        res = await client.put_user_notes(path, new_body, if_match=etag)
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 409:
            return json.dumps({"error": f"wiki request failed: HTTP {e.response.status_code}"})
        # 409 → retry once: fetch fresh etag, re-append on the new base.
        client.invalidate_node(path)
        try:
            node2 = await client.get_node(path)
        except Exception as e2:
            return json.dumps({"error": f"wiki request failed on retry: {e2}"})
        if node2 is None:
            return json.dumps({"error": "node disappeared during retry"})
        prior2 = node2.get("user_notes", "")
        body2 = (prior2.rstrip() + "\n\n" + text + "\n") if prior2 else (text + "\n")
        try:
            res = await client.put_user_notes(path, body2, if_match=node2.get("etag"))
        except httpx.HTTPStatusError as e3:
            if e3.response.status_code == 409:
                return json.dumps({"error": "etag conflict", "path": path})
            return json.dumps({"error": f"wiki request failed: HTTP {e3.response.status_code}"})

    client.invalidate_node(path)
    return json.dumps({"ok": True, "etag": res.get("etag")})


async def _wiki_replace_user_notes_impl(path: str, text: str) -> str:
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

    description = f"Replace Wiki notes at {path}"
    command = (f"PUT /v1/wiki/user-notes path={path} (REPLACE)\n"
               f"→ {_preview(text)}")
    if not await _request_confirm("wiki_replace_notes", description, command):
        return json.dumps({"error": "user declined"})

    etag = node.get("etag")
    try:
        res = await client.put_user_notes(path, text, if_match=etag)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 409:
            # Replace is destructive — do NOT retry. Let LLM re-read and confirm.
            return json.dumps({
                "error": "content modified by others, please review new content first",
                "path": path,
            })
        return json.dumps({"error": f"wiki request failed: HTTP {e.response.status_code}"})

    client.invalidate_node(path)
    return json.dumps({"ok": True, "etag": res.get("etag")})


async def _wiki_register_root_impl(path: str, level: str = "project") -> str:
    if blocked := _gate(path):
        return blocked
    client = WIKI_CLIENT_VAR.get()
    if client is None:
        return json.dumps({"error": "wiki service unavailable"})

    description = f"Register Wiki root: {path} as {level}"
    command = f"POST /v1/wiki/roots path={path} level={level}"
    if not await _request_confirm("wiki_register_root", description, command):
        return json.dumps({"error": "user declined"})

    try:
        res = await client.post_root(path, level)
    except httpx.HTTPStatusError as e:
        body = ""
        try:
            body = e.response.text
        except Exception:
            pass
        return json.dumps({
            "error": f"wiki request failed: HTTP {e.response.status_code}",
            "detail": body,
        })
    return json.dumps({"ok": True, "root_id": res.get("id"), "path": res.get("path")})


@function_tool
async def wiki_append_user_notes(path: str, text: str) -> str:
    """Append text to a Wiki node's user notes. Pops a confirmation to the
    user — they see the path and a preview of `text`.

    Args:
        path: Absolute Wiki node path
        text: Body to append (preserved verbatim, separated from prior notes
            by a blank line)
    """
    return await _wiki_append_user_notes_impl(path, text)


@function_tool
async def wiki_replace_user_notes(path: str, text: str) -> str:
    """Replace the entire user-notes body at a Wiki node. Confirms with the
    user. If someone else modified the notes since you last read them, this
    fails with a clear error — re-read with wiki_get_node and try again.
    """
    return await _wiki_replace_user_notes_impl(path, text)


@function_tool
async def wiki_register_root(path: str, level: str = "project") -> str:
    """Register a new Wiki Root. Confirms with the user.

    IMPORTANT: NimoOS Wiki does NOT auto-discover folders — only paths
    registered via this tool appear in the map. If the user mentions a
    project path that isn't in the system-prompt map, the right behavior
    is usually to ASK whether they'd like to register it (via this tool),
    not to assume it doesn't exist on the NAS.

    Level is usually "project"; "space" is reserved for storage-volume-
    level roots (e.g., the root of a mounted drive like /DATA).
    """
    return await _wiki_register_root_impl(path, level)
