"""Interactive file-access authorization requests.

When a file tool hits a path outside the session's visible_resources (but NOT
a hard-blacklisted one), ask the user to grant access via a card, block on
their decision, and — if granted — add the path to visible_resources so the
original op can be retried. Reuses ConfirmManager + the SSE sink + /confirm,
exactly like the write-op confirmation flow (skills/app_management.py).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import permissions
from audit import audit as _audit

# op category -> human reason shown on the card (English fallback; the UI
# localizes via reason_key, see NimoOS-UI PermissionRequestCard.vue).
_REASON = {
    "list":   "Needs to browse this folder",
    "read":   "Needs to read files inside",
    "write":  "Needs to create or modify files inside",
    "search": "Needs to search its contents",
}
_DEFAULT_REASON = "Needs access to this path to complete your request"

# Single-flight: collapse concurrent requests for the same (session, path)
# onto one card. Keyed (session_id, abs_path) -> asyncio.Future[bool].
_pending_requests: dict[tuple[str, str], asyncio.Future] = {}
# Session-scoped memory of paths the user already denied, so an LLM that
# ignores the system prompt and retries doesn't spam new cards.
_denied: set[tuple[str, str]] = set()


def reset_state() -> None:
    """Test hook: clear in-memory single-flight + denied maps."""
    _pending_requests.clear()
    _denied.clear()


def clear_denied_for_session(session_id: str) -> None:
    """Forget this session's prior denials. Called at the start of each agent
    run so a new user turn (or a Stop, which resolves pending confirms to
    False) does not permanently poison a path. Within a single run, _denied
    still suppresses repeat prompts for an already-denied path."""
    for key in [k for k in _denied if k[0] == session_id]:
        _denied.discard(key)


def _insert_visible_resource(ctx, abs_path: str, kind: str) -> None:
    ctx["conn"].execute(
        "INSERT OR IGNORE INTO visible_resources "
        "(session_id, path, kind, added_at) VALUES (?,?,?,?)",
        (ctx["session_id"], abs_path, kind, int(time.time())),
    )
    ctx["conn"].commit()


def _record_request(ctx, confirm_id: str, abs_path: str, kind: str,
                    reason: str, reason_key: str) -> None:
    """Durably record a new (pending) access request so a refreshed page can
    rebuild the card. decision stays NULL until resolved."""
    ctx["conn"].execute(
        "INSERT INTO access_requests "
        "(confirm_id, session_id, run_id, path, kind, reason, reason_key, decision, created_at) "
        "VALUES (?,?,?,?,?,?,?,NULL,?)",
        (confirm_id, ctx["session_id"], ctx.get("run_id", ""), abs_path, kind, reason,
         reason_key, int(time.time())),
    )
    ctx["conn"].commit()


def _record_decision(ctx, confirm_id: str, decision: str) -> None:
    ctx["conn"].execute(
        "UPDATE access_requests SET decision=?, resolved_at=? WHERE confirm_id=?",
        (decision, int(time.time()), confirm_id),
    )
    ctx["conn"].commit()


def _policy_auto_grants(ctx, abs_paths: list[str]) -> bool:
    """True if the global permission policy waives the access card for EVERY
    path. Belt-and-braces: even under an auto policy a path that resolves into
    a system location (FS_DENY_ROOTS, "/") still gets a card — the blacklist
    upstream should have caught it, but an auto-grant must never be the thing
    that widens what a human click would have been asked about."""
    try:
        if not permissions.auto_approve(ctx["conn"], "grant_access"):
            return False
        from tasks.driver import fs_root_denied  # noqa: PLC0415 — avoid cycle
        for p in abs_paths:
            if fs_root_denied(os.path.realpath(p)):
                return False
        return True
    except Exception:  # noqa: BLE001 — fail toward asking
        return False


def _auto_grant(ctx, abs_paths: list[str], kind_hint: str, reason: str,
                reason_key: str) -> None:
    """Persist grants + a resolved access_requests row (so rebuilt history
    still shows what was opened) + an audit record. No card is emitted."""
    confirm_id = str(uuid.uuid4())
    path_field = abs_paths[0] if len(abs_paths) == 1 else json.dumps(abs_paths)
    _record_request(ctx, confirm_id, path_field, kind_hint, reason, reason_key)
    _record_decision(ctx, confirm_id, "granted")
    for p in abs_paths:
        _insert_visible_resource(ctx, p, _infer_kind(p))
    try:
        _audit("grant_access", session_id=ctx.get("session_id"),
               paths=abs_paths, decision="auto_approved_by_policy")
    except Exception:  # noqa: BLE001 — auditing must never break the grant
        pass


async def request_access(ctx, abs_path: str, kind: str, op: str) -> bool:
    """Ask the user to authorize abs_path for this session. Returns True if
    granted (and writes visible_resources), False otherwise. The request and
    its decision are recorded in access_requests so a refreshed page can
    rebuild the (resolved) card."""
    session_id = ctx["session_id"]
    cache_key = (session_id, abs_path)

    if cache_key in _denied:
        return False
    if cache_key in _pending_requests:
        return await _pending_requests[cache_key]

    reason = _REASON.get(op, _DEFAULT_REASON)
    reason_key = op if op in _REASON else "default"
    if _policy_auto_grants(ctx, [abs_path]):
        _auto_grant(ctx, [abs_path], kind, reason, reason_key)
        return True

    mgr = ctx["confirm_mgr"]
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending_requests[cache_key] = fut
    confirm_id = None
    try:
        confirm_id = mgr.register(session_id, "grant_access", abs_path, "")
        _record_request(ctx, confirm_id, abs_path, kind, reason, reason_key)
        await ctx["sink"].put({
            "type": "access_request",
            "confirm_id": confirm_id,
            "path": abs_path,
            "kind": kind,
            "reason": reason,
            "reason_key": reason_key,
        })
        granted = await mgr.wait(confirm_id)
        _record_decision(ctx, confirm_id, "granted" if granted else "denied")
        if granted:
            _insert_visible_resource(ctx, abs_path, kind)   # persist first
        else:
            _denied.add(cache_key)
        if not fut.done():
            fut.set_result(granted)                          # then broadcast to concurrent waiters
        return granted
    except BaseException as e:
        # Cancelled/interrupted before a decision: mark the row so no NULL
        # orphan lingers. Guarded — a failure here must not mask `e`.
        if confirm_id is not None:
            try:
                _record_decision(ctx, confirm_id, "cancelled")
            except Exception:
                pass
        if not fut.done():
            fut.set_exception(e)                             # broadcast the exception too, to avoid a deadlock
        raise
    finally:
        _pending_requests.pop(cache_key, None)               # must always clean up


def _infer_kind(abs_path: str) -> str:
    """Return 'folder' for directories (and non-existent paths, treated as future dirs),
    'file' for existing files. Matches the anchoring logic in ops._candidate_for_request."""
    if os.path.isfile(abs_path):
        return "file"
    return "folder"


async def request_access_batch(ctx, abs_paths: list[str], op: str) -> bool:
    """Emit ONE access-request card covering all abs_paths; the user approves or
    denies the whole set atomically. Returns True only on approval. On approval,
    every path is persisted as a grant in visible_resources using the SAME
    persistence logic as request_access (folder grant for dirs / nearest folder
    for files — match whatever request_access does). Returns True immediately for
    an empty list. Returns False if ctx['confirm_mgr'] is None (headless)."""
    if not abs_paths:
        return True

    mgr = ctx["confirm_mgr"]
    if mgr is None:
        return False

    session_id = ctx["session_id"]
    reason = _REASON.get(op, _DEFAULT_REASON)
    reason_key = op if op in _REASON else "default"
    if _policy_auto_grants(ctx, list(abs_paths)):
        _auto_grant(ctx, list(abs_paths), "folder", reason, reason_key)
        return True
    confirm_id = None
    try:
        confirm_id = mgr.register(session_id, "grant_access", abs_paths[0], "")
        # Store one DB row for the whole batch. The `path` column holds a
        # JSON-encoded list so a refreshed page can rebuild the multi-path card.
        # Using INSERT OR IGNORE keeps `_record_request` safe if somehow called
        # twice (though that cannot happen here).
        _record_request(ctx, confirm_id, json.dumps(abs_paths), "folder", reason, reason_key)
        await ctx["sink"].put({
            "type": "access_request",
            "confirm_id": confirm_id,
            "paths": abs_paths,
            # Keep a single `path` field for backwards-compatible consumers that
            # only look at the first path.
            "path": abs_paths[0],
            "kind": "folder",
            "reason": reason,
            "reason_key": reason_key,
        })
        granted = await mgr.wait(confirm_id)
        _record_decision(ctx, confirm_id, "granted" if granted else "denied")
        if granted:
            for p in abs_paths:
                kind = _infer_kind(p)
                _insert_visible_resource(ctx, p, kind)
        return granted
    except BaseException as e:
        if confirm_id is not None:
            try:
                _record_decision(ctx, confirm_id, "cancelled")
            except Exception:
                pass
        raise
