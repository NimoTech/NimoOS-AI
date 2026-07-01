"""Headless, deny-only path gate for MCP read tools. Replaces the chat agent's
session-based visible_resources gate (which needs a conversation + can pop an
interactive access-request card — neither exists for a headless MCP call).

Allows only paths inside the user-visible root (/DATA) and never the hidden
system-data dir. realpath() resolves `..` and symlinks so traversal/symlink
escapes are caught. Raises McpPathDenied on any out-of-scope path; never
prompts, never falls back to a default scope."""
from __future__ import annotations

import os


class McpPathDenied(Exception):
    """Raised when a requested path is outside the allowed MCP read scope."""


def mcp_resolve_read_path(path: str, root: str = "/DATA") -> str:
    if not path:
        raise McpPathDenied("empty path")
    real = os.path.realpath(path)                      # resolves .. and symlinks
    root_real = os.path.realpath(root).rstrip(os.sep)  # normalize trailing slash
    if real != root_real and not real.startswith(root_real + os.sep):
        raise McpPathDenied(f"path outside {root}")
    sysdata = os.path.join(root_real, ".system_data")
    if real == sysdata or real.startswith(sysdata + os.sep):
        raise McpPathDenied("path in system-data")
    return real
