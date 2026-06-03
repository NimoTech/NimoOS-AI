"""Interactive file-access authorization requests.

When a file tool hits a path outside the session's visible_resources (but NOT
a hard-blacklisted one), ask the user to grant access via a card, block on
their decision, and — if granted — add the path to visible_resources so the
original op can be retried. Reuses ConfirmManager + the SSE sink + /confirm,
exactly like the write-op confirmation flow (skills/app_management.py).
"""
from __future__ import annotations

import asyncio
import time

# op category -> human reason shown on the card.
_REASON = {
    "list":   "需要浏览该文件夹",
    "read":   "需要读取其中的文件",
    "write":  "需要在其中创建或修改文件",
    "search": "需要在其中检索内容",
}
_DEFAULT_REASON = "需要访问该路径以完成你请求的操作"

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


def _record_request(ctx, confirm_id: str, abs_path: str, kind: str, reason: str) -> None:
    """Durably record a new (pending) access request so a refreshed page can
    rebuild the card. decision stays NULL until resolved."""
    ctx["conn"].execute(
        "INSERT INTO access_requests "
        "(confirm_id, session_id, run_id, path, kind, reason, decision, created_at) "
        "VALUES (?,?,?,?,?,?,NULL,?)",
        (confirm_id, ctx["session_id"], ctx.get("run_id", ""), abs_path, kind, reason,
         int(time.time())),
    )
    ctx["conn"].commit()


def _record_decision(ctx, confirm_id: str, decision: str) -> None:
    ctx["conn"].execute(
        "UPDATE access_requests SET decision=?, resolved_at=? WHERE confirm_id=?",
        (decision, int(time.time()), confirm_id),
    )
    ctx["conn"].commit()


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

    mgr = ctx["confirm_mgr"]
    reason = _REASON.get(op, _DEFAULT_REASON)
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending_requests[cache_key] = fut
    confirm_id = None
    try:
        confirm_id = mgr.register(session_id, "grant_access", abs_path, "")
        _record_request(ctx, confirm_id, abs_path, kind, reason)
        await ctx["sink"].put({
            "type": "access_request",
            "confirm_id": confirm_id,
            "path": abs_path,
            "kind": kind,
            "reason": reason,
        })
        granted = await mgr.wait(confirm_id)
        _record_decision(ctx, confirm_id, "granted" if granted else "denied")
        if granted:
            _insert_visible_resource(ctx, abs_path, kind)   # 先落库
        else:
            _denied.add(cache_key)
        if not fut.done():
            fut.set_result(granted)                          # 再广播给并发等待者
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
            fut.set_exception(e)                             # 异常也广播,防死锁
        raise
    finally:
        _pending_requests.pop(cache_key, None)               # 必清理
