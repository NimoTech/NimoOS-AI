"""Per-MCP-server gating: handle resolution and the L2 unlock gate.

Each connected MCP server gets its own expand_tools gate, on top of (not
instead of) the static "mcp" category from tool_gating.py. The two levels are:

  L1 - expand_tools(["mcp"]) unlocks the MCP management tools (add_mcp_server
       etc.) and prints a catalogue: which tool names each connected server
       offers, with no JSON schema attached.
  L2 - expand_tools(["mcp:github"]) unlocks and injects the FunctionTools of
       that one server.

Why split the two: every MCP server used to share the single "mcp" gate, so
opening it dumped N servers x M tools of full JSON schema into the prompt at
once (roughly 20k tokens for a single 87-tool server). Now "mcp" alone never
loads any server's schema; only naming a specific server via "mcp:<handle>"
does. This module owns the gate-key/handle bookkeeping for that split; the
actual FunctionTool loading is done in skills/tool_gating.py's
_load_l2_tools_async (Task 16), which appends the loaded tools directly onto
the live run's agent.tools rather than gating them with an is_enabled
callback keyed on gate_key() — by the time a tool exists in agent.tools at
all, its server's gate is already open (that IS the gate), so no separate
per-tool enable check is needed.
"""
from __future__ import annotations

from contextvars import ContextVar

# Injected at the start of a run: slug -> server_id, the inverse of
# mcp_client.client.assign_slugs(servers) (id -> slug). Defaults to an empty
# dict as a fallback (tests/error paths).
MCP_HANDLES_VAR: ContextVar[dict[str, int]] = ContextVar("nimoos_mcp_handles")


def gate_key(server_id: int) -> str:
    """Gate key for one MCP server's unlocked state.

    Keyed by server id, not slug: slugs are derived from mutable data (a
    server's display name), so a persisted slug-keyed unlock would let a
    renamed or newly created server inherit a previous server's unlocked
    state.
    """
    return f"mcp#{server_id}"


def resolve_handle(token: str) -> int | None:
    """Resolve an "mcp:<handle>" token to a server id, or None if unknown."""
    slug = token.split(":", 1)[1] if ":" in token else token
    return MCP_HANDLES_VAR.get({}).get(slug)
