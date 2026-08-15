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
    slug: str = ""                                    # this server's actual per-run deduped
                                                        # expand_tools token from assign_slugs
                                                        # (mcp_client.client); populated at every
                                                        # construction site since Task 16. The
                                                        # ONLY value guaranteed correct after a
                                                        # handle collision (e.g. "github_2") --
                                                        # see _slug_token.
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
_INSTRUCTIONS_MAX = 4000     # generous full-paragraph budget for L2's instructions —
                             # bigger than _DETAIL_MAX's one-liner budget on purpose,
                             # but still bounded against a runaway/hostile blob

FALLBACK_LINE = "MCP runtime tools, if any, appear in your tool list on the next step."


def _short(text, max_len: int = _DETAIL_MAX) -> str:
    """Collapse all whitespace (including embedded newlines) to single spaces
    and cap length. Applied to every piece of third-party server-reported
    text (`detail`, `summary`, `instructions`) before it reaches the model:
    without the whitespace collapse, an embedded newline in e.g. `summary`
    can make one server's L1 entry visually split into what looks like a
    second, independently parseable "MCP server ..." line — see the
    trailing-";" boundary convention in render_expand_section, which this
    normalization protects.
    """
    d = " ".join(str(text or "").split())
    return d if len(d) <= max_len else d[:max_len - 1] + "…"


def _label(s: ServerStatus) -> str:
    """The name the MODEL sees for this server.

    Preference order:
      1. `handle` -- the self-reported identity (Task 7). Best: it's what
         the model uses elsewhere too.
      2. `slug` -- the deduped assign_slugs identifier. Still model-facing
         and collision-safe even before a server has ever been probed,
         since assign_slugs derives it from the name when there's no handle
         yet (`_slug(name)`).
      3. `name` -- the raw user-typed name (e.g. "测试1" / "test 1"), which
         the model should never see per the "reader is the model, not the
         user" rule. This is a deliberate, documented exception: it only
         fires when a status object has NEITHER a handle NOR a slug (e.g.
         hand-built without ever going through assign_slugs), and at that
         point there is no better model-facing identifier available --
         showing nothing would be worse than showing the name.
    """
    return s.handle or s.slug or s.name


def _slug_token(s: ServerStatus) -> str:
    """The exact `expand_tools(["mcp:<token>"])` token for this server, or
    "" when no trustworthy slug is available (callers must then SUPPRESS the
    "expand as:" hint entirely rather than guess).

    Preference order:
      1. `s.slug` -- the actual per-run deduped slug from assign_slugs
         (mcp_client.client.assign_slugs). The only value guaranteed to
         reflect a collision bump (e.g. "github_2"); Task 16 populates it.
      2. Recovered from an already-fq tool name (`mcp__<slug>__<tool>`, see
         client._wrap_tool) when `s.slug` isn't set yet but tool names are.
      3. "" -- deliberately NOT `_label(s)`. Falling back to the un-deduped
         handle/name here was the bug: a server whose handle collided with
         a sibling's (and so was actually assigned e.g. "github_2") would
         advertise the sibling's bare "github" token, silently pointing
         `expand_tools` at the WRONG server -- worse than advertising
         nothing at all.
    """
    if s.slug:
        return s.slug
    if s.tool_names:
        parts = s.tool_names[0].split("__")
        if len(parts) >= 3 and parts[0] == "mcp":
            return parts[1]
    return ""


def _summary(s: ServerStatus) -> str:
    label = _label(s)
    if s.status == OK:
        n = len(s.tool_names)
        if n == 0:
            # Plainly distinct from a real "N tools ready" line: the server
            # connected fine, it simply has nothing to offer right now (the
            # common real-world case is auth the server needs but this repo
            # deliberately does not try to detect — see render_expand_section).
            # "0 tools ready" would read as a working, expandable server.
            return f"{label}: connected, published no tools"
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
        if s.status == OK and not s.tool_names:
            # A server that connected fine but published zero tools is NOT
            # the same state as one with tools ready to expand. Left to fall
            # into the branch below, `listed` would be "" -- producing a
            # malformed "... : ;" line -- and the expand hint would invite the
            # model to open a gate with nothing behind it; following that
            # advice used to get told "no tool schemas could be loaded right
            # now -- try again shortly", which is false (there is nothing to
            # load, and no amount of retrying will change that). This is not
            # hypothetical -- an authenticated remote server that probes ok
            # but requires a login it doesn't have publishes exactly this.
            # Deliberately not attempting to detect auth here (accepted
            # limitation) -- just state the observed fact and stop there, with
            # no "expand as:" hint.
            summary_part = f" — {_short(s.summary)}" if s.summary else ""
            lines.append(f'MCP server "{label}": connected, but published no tools{summary_part}.')
        elif s.status == OK:
            # list EVERY tool — an elided "… (40 total)" left the model unable
            # to see (and thus call) the hidden names, so it drifted to other tools
            listed = ", ".join(s.tool_names)
            summary_part = f" — {_short(s.summary)}" if s.summary else ""
            # No schema and not the full instructions here — just enough to
            # route: which tools exist, and the exact token that opens L2
            # (mcp_gating.resolve_handle only accepts the deduped slug, which
            # _slug_token recovers from s.slug or the tool names themselves).
            token = _slug_token(s)
            # No trustworthy slug (see _slug_token) -> suppress the hint
            # rather than guess: advertising no token beats advertising a
            # wrong one that resolves to a different server.
            expand_hint = f" expand as: mcp:{token} for full tool schemas;" if token else ""
            # trailing ";" terminates this server's tool list — without it,
            # weaker models read the unbulleted server lines as sub-items of
            # the preceding tool instead of parallel lists of callable tools
            lines.append(f'MCP server "{label}" ({len(s.tool_names)} tools){summary_part}: '
                         f'{listed};{expand_hint}')
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
                token = _slug_token(s)
                expand_hint = f" expand as: mcp:{token} for details;" if token else ""
                line += (f" — its last known tools{stale_note}: "
                        f"{', '.join(s.tool_names)};{expand_hint}")
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
        parts.append(_short(s.summary))
    if s.stale:
        # Still surfaced, not hidden: the model asked to open this specific
        # server, so tell it plainly that what follows may be out of date
        # rather than silently serving cached text as if it were live.
        detail_part = f" ({_short(s.detail)})" if s.detail else ""
        parts.append(f"Note: this server is currently degraded{detail_part}; "
                     "the following instructions may be stale.")
    if s.instructions:
        # Third-party text from the server itself: whitespace-normalized and
        # length-capped the same way `detail`/`summary` are (see _short),
        # just with a paragraph-sized budget instead of a one-liner's.
        parts.append(_short(s.instructions, _INSTRUCTIONS_MAX))
    return "\n\n".join(parts)
