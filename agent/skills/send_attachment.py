"""Channel-only tool: send a file from the user's authorized storage back to
the chat. Registered ONLY for channel-sourced sessions (see
agent.py::select_tools_for_run, gated on sessions.source != 'web'). Sends
synchronously via an injected per-run callback (wired in Task B3) and returns
the REAL success/failure to the model — never a false "sent"."""
from __future__ import annotations

from contextvars import ContextVar
from typing import Awaitable, Callable, Optional

from agents import function_tool

# Set per-run: async (path, caption) -> message_id. None outside channel runs
# (or before Task B3 wires the channel adapter's send_file in).
SEND_FILE_VAR: ContextVar = ContextVar("chan_send_file", default=None)

# Set per-run by agent.py's central ContextVar block (Step 6).
SESSION_ID_VAR: ContextVar[str] = ContextVar("chan_send_session", default="")


def _default_validate(path: str) -> Optional[str]:
    """Resolve+gate `path` through the SAME fs authorization boundary
    read_document/read_file use for a `path` argument
    (skills/search/search.py::_read_document_impl -> fs.ops.
    _resolve_and_gate_or_request, built on skills.filesystem._ctx()).

    We call the non-interactive half of that chain, fs.ops._resolve_and_gate
    (paths.resolve — realpath + scope-check against visible_resources — then
    ignore.gate — hard-blacklist / gitignore rules), and skip the
    interactive access-request escalation _resolve_and_gate_or_request falls
    back to on out-of-scope paths: an outbound send must never pop a
    scope-grant card, it should just be refused. Only paths that resolve
    (after realpath, so `..`-traversal and symlink escapes are caught) into
    one of the session's granted `visible_resources` are allowed; anything
    else — including `.system_data` and other out-of-scope paths — is
    denied simply because it falls outside that granted set, not by a
    dedicated `.system_data` rule.

    Returns the real absolute path if allowed, else None. Fails CLOSED:
    never raises, and any unexpected error is treated as a denial.
    """
    from fs import ops as _fsops, paths as _fspaths, ignore as _fsignore
    import skills.filesystem as _fsskill

    session_id = SESSION_ID_VAR.get()
    if not session_id:
        return None
    try:
        conn = _fsskill.DB_VAR.get()
    except LookupError:
        return None
    ctx = {
        "session_id": session_id,
        "conn": conn,
        "user_patterns": _fsskill.USER_PATTERNS_VAR.get([]),
    }
    try:
        return _fsops._resolve_and_gate(ctx, path)
    except (_fspaths.PermissionDenied, _fsignore.BlockedImplicit,
            _fsignore.BlockedHardBlacklist, _fsignore.BlockedGitignore):
        return None
    except Exception:
        # Deny-closed: any unexpected error (e.g. a transient sqlite error)
        # must never propagate or fall through to treating `path` as
        # authorized — fail the same way an explicit gate rejection does.
        return None


async def _send_attachment_impl(
        path: str, caption: str, *,
        send_file: Optional[Callable[[str, str], Awaitable[str]]],
        validate=_default_validate) -> str:
    real = validate(path)
    if real is None:
        return f"error: path not allowed or not found: {path}"
    if send_file is None:
        return "error: sending files is not available in this session"
    try:
        mid = await send_file(real, caption or "")
    except Exception as e:
        return f"error: failed to send file: {e}"
    return f"ok: file sent (message id {mid})"


@function_tool
async def send_attachment(path: str, caption: str = "") -> str:
    """Send a file from the user's storage to the current chat as an
    attachment.

    The file is delivered to the user the MOMENT you call this tool — before
    your text reply, which is only sent once, after the whole turn ends. So
    do not write "see the file below/above" or similar: by the time your
    text arrives the file will already be there (or already have failed to
    send, per this tool's return value). Just describe the file you sent.

    Args:
        path: Absolute path to the file, within your authorized storage
            scope (the same scope read_file/read_document use).
        caption: Optional short caption to attach to the file.
    """
    return await _send_attachment_impl(
        path, caption, send_file=SEND_FILE_VAR.get())
