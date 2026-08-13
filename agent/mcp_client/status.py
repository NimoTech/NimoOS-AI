"""Per-run MCP server status snapshot (defects 1A/2A share one snapshot).

Single source of truth for BOTH consumers:
- the one-line status appended to the system prompt (render_prompt_line) —
  a routing signal: server name + status + tool count, ~20-40 tokens;
- the expand_tools(["mcp"]) return text (render_expand_section) —
  action-level detail: real tool names + failure reasons.

Deliberately dependency-free (no SDK imports): skills/tool_gating.py imports
this from inside the skills package and must not drag the MCP SDK along.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field

OK = "ok"
FAILED = "failed"
WARMING = "warming"
CONFIG_ERROR = "config_error"


@dataclass
class ServerStatus:
    name: str
    status: str                                       # OK | FAILED | WARMING | CONFIG_ERROR
    detail: str = ""                                  # failure reason; empty when OK
    tool_names: list = field(default_factory=list)    # fq tool names (post-dedup)


@dataclass
class McpStatusSnapshot:
    servers: list = field(default_factory=list)       # list[ServerStatus]
    config_error: str = ""    # non-empty: the runtime config fetch itself failed —
                              # distinct from "no servers configured" (empty servers)


# Set per run by agent.py (same *_VAR isolation pattern as the rest of the repo).
# None = MCP not in play this run, or the snapshot was lost to an error.
MCP_STATUS_VAR: ContextVar = ContextVar("mcp_status_snapshot", default=None)

_DETAIL_MAX = 80
_EXPAND_TOOLS_MAX = 15

FALLBACK_LINE = "MCP runtime tools, if any, appear in your tool list on the next step."


def _short(detail) -> str:
    d = " ".join(str(detail or "").split())
    return d if len(d) <= _DETAIL_MAX else d[:_DETAIL_MAX - 1] + "…"


def _summary(s: ServerStatus) -> str:
    if s.status == OK:
        n = len(s.tool_names)
        return f"{s.name}: {n} tool{'s' if n != 1 else ''} ready"
    if s.status == WARMING:
        return f"{s.name}: starting up, tools available shortly"
    if s.status == CONFIG_ERROR:
        return f"{s.name}: configuration error ({_short(s.detail)})"
    return f"{s.name}: failed to load ({_short(s.detail)})"


def render_prompt_line(snapshot) -> str:
    """One system-prompt line for first-turn routing. "" = inject nothing."""
    if snapshot is None:
        return ""
    if snapshot.config_error:
        return ("[MCP servers: configuration could not be fetched this run "
                f"({_short(snapshot.config_error)}) — configured servers are "
                "temporarily unavailable; do not register new ones to work around it]")
    if not snapshot.servers:
        return ""
    return "[MCP servers: " + "; ".join(_summary(s) for s in snapshot.servers) + "]"


def render_expand_section(snapshot) -> list[str]:
    """Action-level lines appended to expand_tools(["mcp"])'s return text."""
    if snapshot is None:
        return [FALLBACK_LINE]
    if snapshot.config_error:
        return [("MCP server configuration could not be fetched this run "
                 f"({_short(snapshot.config_error)}). Configured servers may exist "
                 "but are temporarily unavailable — report this to the user instead "
                 "of registering new servers.")]
    if not snapshot.servers:
        return ["No MCP servers are configured."]
    lines = []
    degraded = False
    for s in snapshot.servers:
        if s.status == OK:
            listed = ", ".join(s.tool_names[:_EXPAND_TOOLS_MAX])
            if len(s.tool_names) > _EXPAND_TOOLS_MAX:
                listed += f", … ({len(s.tool_names)} total)"
            lines.append(f'MCP server "{s.name}" ({len(s.tool_names)} tools): {listed}')
        elif s.status == WARMING:
            lines.append(f'MCP server "{s.name}": starting up in the background; '
                         "its tools should appear on a later message.")
        else:
            degraded = True
            kind = ("configuration error" if s.status == CONFIG_ERROR
                    else "failed to load")
            lines.append(f'MCP server "{s.name}": {kind} ({_short(s.detail)}) — '
                         "its tools are unavailable this run.")
    if degraded:
        lines.append("Do not register replacement MCP servers for ones that "
                     "failed — report the failure to the user instead.")
    return lines
