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
actual FunctionTool loading lives in skills/tool_gating.py, which builds a
server's tools only when its gate is open rather than gating always-built
tools with an is_enabled callback keyed on gate_key(). Selection at BUILD time
is still deliberate even though estimate_tools_tokens now skips
is_enabled=False tools: building only open-gated servers keeps the schema
cache and the "a tool exists in agent.tools ⇒ its gate is open" invariant
below, and avoids fetching N servers' schemas that nothing will send.

L2 loading therefore has two entry points, sharing tool_gating._fetch_and_build:
  - _load_l2_tools_async — the gate's FIRST opening, mid-run: splice onto the
    live run's agent.tools, which the SDK re-reads every step;
  - rehydrate_unlocked_mcp_tools — every LATER run in the same session: the
    gate persisted, but that Agent object did not. "A tool exists in
    agent.tools, therefore its gate is open" holds within one run and was
    originally treated as the whole gate; across runs the converse silently
    failed, and calling such a tool raised ModelBehaviorError "Tool
    mcp__<slug>__<tool> not found in agent".
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


TOOL_NAME_PREFIX = "mcp__"


def tool_not_found_message(tool_name: str) -> str | None:
    """Model-facing recovery text when the model calls an `mcp__*` tool that
    isn't in this run's tool list; None for any non-MCP name (the SDK's own
    "Tool 'x' not found." is right for those).

    Reachable even with run-start rehydration in place: the fetch can degrade
    to (0, []), the write token can be rejected, the server can have been
    deleted or disabled between two messages, or the model can simply invent a
    name. Without this the call raises ModelBehaviorError and kills the whole
    turn; with it the model is told which gate to open and can fix itself in
    one step (see phoenix_tracing.build_trace_run_config, which wires it to
    RunConfig.tool_error_formatter alongside
    tool_not_found_behavior="return_error_to_model").

    The slug is recovered by matching against THIS run's known slugs, longest
    first — never by splitting on "__". Both slugs and tool names may contain
    underscores ("github_2" beside a server whose tool is named "_2__x"), so
    the "__" boundary is genuinely ambiguous; MCP_HANDLES_VAR is the only
    authority on where the slug ends.
    """
    if not tool_name.startswith(TOOL_NAME_PREFIX):
        return None
    handles = MCP_HANDLES_VAR.get({}) or {}
    for slug in sorted(handles, key=len, reverse=True):
        if tool_name.startswith(f"{TOOL_NAME_PREFIX}{slug}__"):
            return (f"Tool {tool_name} is not loaded in this conversation turn. "
                    f'Call expand_tools(["mcp:{slug}"]) first — that puts this '
                    "server's tools in your tool list — then call it again with "
                    "the same arguments.")
    # An mcp__ name whose slug matches no connected server this run: do not
    # point at a gate that cannot be opened, send the model back to the
    # catalogue instead.
    return (f"Tool {tool_name} does not belong to any MCP server connected in "
            'this conversation. Call expand_tools(["mcp"]) to see which servers '
            "are connected and how to load their tools.")
