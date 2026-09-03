"""Headless, deny-only path gate for MCP read tools. Replaces the chat agent's
session-based visible_resources gate (which needs a conversation + can pop an
interactive access-request card — neither exists for a headless MCP call).

Rules, all deny-only, never prompting, never widening to a default scope:

1. The path must resolve (realpath: `..` and symlinks) inside `root`
   (/DATA, the user-visible root) and never inside `<root>/.system_data`.
2. The chat agent's implicit-ignore and hard-blacklist layers apply
   (`fs.ignore.gate`): `.ssh/`, `*.pem`, `id_rsa*`, `agent.db`, ... are
   denied wherever they sit. The MCP surface previously skipped this layer.
3. Knowledge notes live per user under `<notes_root>/<user_id>/`. A path
   inside notes_root is readable only when it sits under the caller's own
   subtree; without a known caller identity nothing under notes_root is
   readable. This mirrors the SQL-side isolation in notes.store so the path
   route cannot be used to read another user's notes.
"""
from __future__ import annotations

import os

from fs import ignore


class McpPathDenied(Exception):
    """Raised when a requested path is outside the allowed MCP read scope."""


def _within(real: str, base: str) -> bool:
    return real == base or real.startswith(base + os.sep)


def mcp_resolve_read_path(path: str, root: str = "/DATA", *,
                          user_id: str | None = None,
                          notes_root: str | None = None) -> str:
    if not path:
        raise McpPathDenied("empty path")
    real = os.path.realpath(path)                      # resolves .. and symlinks
    root_real = os.path.realpath(root).rstrip(os.sep)  # normalize trailing slash
    if not _within(real, root_real):
        raise McpPathDenied(f"path outside {root}")
    sysdata = os.path.join(root_real, ".system_data")
    if _within(real, sysdata):
        raise McpPathDenied("path in system-data")

    # Same implicit-ignore / hard-blacklist layers the chat agent enforces.
    # No visible roots and no user patterns here: only the built-in layers
    # (the .gitignore layer needs a visible root and is skipped).
    try:
        ignore.gate(real, [], [])
    except (ignore.BlockedImplicit, ignore.BlockedHardBlacklist) as e:
        raise McpPathDenied(f"blacklisted path: {e}") from None

    if notes_root:
        notes_real = os.path.realpath(notes_root).rstrip(os.sep)
        if _within(real, notes_real):
            if not user_id:
                raise McpPathDenied("notes require a known caller identity")
            if real == notes_real:
                raise McpPathDenied("notes root is not readable")
            if not _within(real, os.path.join(notes_real, str(user_id))):
                raise McpPathDenied("notes of another user")

    tool_outputs = os.path.join(root_real, "AppData", "nimoos-agent", "tool-outputs")
    if real == tool_outputs or real.startswith(tool_outputs + os.sep):
        raise McpPathDenied("path in tool-output offload dir")
    return real
