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
    handle: str = ""                                  # self-reported identity (Task 7); "" = never probed
    summary: str = ""                                 # server's one-line self-description (L1)
    instructions: str = ""                             # server's full instructions text (L2 only)
    stale: bool = False                                # tool_names/instructions are cached from
                                                        # before the current failure, not live


@dataclass
class McpStatusSnapshot:
    servers: list = field(default_factory=list)       # list[ServerStatus]
    config_error: str = ""    # non-empty: the runtime config fetch itself failed —
                              # distinct from "no servers configured" (empty servers)


# Set per run by agent.py (same *_VAR isolation pattern as the rest of the repo).
# None = MCP not in play this run, or the snapshot was lost to an error.
MCP_STATUS_VAR: ContextVar = ContextVar("mcp_status_snapshot", default=None)

_DETAIL_MAX = 80

FALLBACK_LINE = "MCP runtime tools, if any, appear in your tool list on the next step."


def _short(detail) -> str:
    d = " ".join(str(detail or "").split())
    return d if len(d) <= _DETAIL_MAX else d[:_DETAIL_MAX - 1] + "…"


def _label(s: ServerStatus) -> str:
    """The name the MODEL sees for this server.

    Prefers the self-reported `handle` over the user-typed `name`: the model
    routes by handle (assign_slugs prefers it too, and L1's "expand as:
    mcp:<handle>" token is built from it), and it never sees the settings
    page where the user typed a name like "测试1" ("test 1"). Falls back to
    `name` only for a server that has never been successfully probed and so
    has no self-reported handle yet.
    """
    return s.handle or s.name


def _slug_token(s: ServerStatus) -> str:
    """The exact `expand_tools(["mcp:<token>"])` token for this server.

    Not re-derived from name/handle here — that would risk drifting from
    assign_slugs's per-run collision dedup (mcp_client.client.assign_slugs),
    which is the sole owner of that logic. Each fq tool name is already
    stamped with the deduped slug (`mcp__<slug>__<tool>`, see
    client._wrap_tool), so when at least one tool name exists we recover the
    real slug straight from it — this is how a server whose handle collided
    and got bumped to e.g. "github_2" still advertises the correct token.
    Only a server with no tool names yet (never successfully probed) falls
    back to the un-deduped label.
    """
    if s.tool_names:
        parts = s.tool_names[0].split("__")
        if len(parts) >= 3 and parts[0] == "mcp":
            return parts[1]
    return _label(s)


def _summary(s: ServerStatus) -> str:
    label = _label(s)
    if s.status == OK:
        n = len(s.tool_names)
        return f"{label}: {n} tool{'s' if n != 1 else ''} ready"
    if s.status == WARMING:
        return f"{label}: starting up, tools available shortly"
    stale_tag = " [stale]" if s.stale else ""
    if s.status == CONFIG_ERROR:
        return f"{label}: configuration error ({_short(s.detail)}){stale_tag}"
    return f"{label}: failed to load ({_short(s.detail)}){stale_tag}"


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
        label = _label(s)
        if s.status == OK:
            # list EVERY tool — an elided "… (40 total)" left the model unable
            # to see (and thus call) the hidden names, so it drifted to other tools
            listed = ", ".join(s.tool_names)
            summary_part = f" — {s.summary}" if s.summary else ""
            # No schema and not the full instructions here — just enough to
            # route: which tools exist, and the exact token that opens L2
            # (mcp_gating.resolve_handle only accepts the deduped slug, which
            # _slug_token recovers from the tool names themselves).
            # trailing ";" terminates this server's tool list — without it,
            # weaker models read the unbulleted server lines as sub-items of
            # the preceding tool instead of parallel lists of callable tools
            lines.append(f'MCP server "{label}" ({len(s.tool_names)} tools){summary_part}: '
                         f'{listed}; expand as: mcp:{_slug_token(s)} for full tool schemas;')
        elif s.status == WARMING:
            lines.append(f'MCP server "{label}": starting up in the background; '
                         "its tools should appear on a later message.")
        else:
            degraded = True
            kind = ("configuration error" if s.status == CONFIG_ERROR
                    else "failed to load")
            line = f'MCP server "{label}": {kind} ({_short(s.detail)})'
            if s.tool_names:
                # A broken server is shown, not hidden: knowing "this server
                # exists and offers create_issue/list_prs, but is currently
                # broken" lets the model explain the gap to the user instead
                # of claiming it has no such capability at all. These names
                # come from the last successful probe, so they are marked
                # stale rather than presented as currently callable.
                stale_note = " (stale — from before the current failure)" if s.stale else ""
                line += (f" — its last known tools{stale_note}: "
                        f"{', '.join(s.tool_names)}; expand as: mcp:{_slug_token(s)}")
            else:
                line += " — its tools are unavailable this run."
            lines.append(line)
    if degraded:
        lines.append("Do not register replacement MCP servers for ones that "
                     "failed — report the failure to the user instead.")
    return lines


def render_l2_preamble(s: ServerStatus) -> str:
    """Text returned when a single server (`expand_tools(["mcp:<handle>"])`)
    is opened. This is the ONLY place a server's full `instructions` belong:
    concatenating them into every one of its tools' descriptions instead
    would repeat the same server-level paragraph once per tool — 87 times
    for an 87-tool server — for text that only needs to be read once, at the
    moment the server is opened.
    """
    label = _label(s)
    parts = [f'MCP server "{label}"']
    if s.summary:
        parts.append(s.summary)
    if s.stale:
        # Still surfaced, not hidden: the model asked to open this specific
        # server, so tell it plainly that what follows may be out of date
        # rather than silently serving cached text as if it were live.
        detail_part = f" ({_short(s.detail)})" if s.detail else ""
        parts.append(f"Note: this server is currently degraded{detail_part}; "
                     "the following instructions may be stale.")
    if s.instructions:
        parts.append(s.instructions)
    return "\n\n".join(parts)
