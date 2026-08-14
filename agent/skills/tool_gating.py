"""Runtime gating primitives for progressive tool exposure.

The unlocked set lives in a ContextVar (same concurrency-isolation pattern as
the other *_VAR globals in agent.py): each run does UNLOCKED_VAR.set(<set
loaded from the session>) at the start, expand_tools updates it in place, and
the is_enabled callback of non-always-on tools reads it to decide whether
they're visible to the model.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Any, Callable

# Injected by agent.py at the start of a run; defaults to an empty set as a
# fallback (tests/error paths).
UNLOCKED_VAR: ContextVar[set[str]] = ContextVar("nimoos_unlocked_categories")
GATING_SESSION_VAR: ContextVar[str] = ContextVar("nimoos_gating_session")


def current_unlocked() -> set[str]:
    return UNLOCKED_VAR.get(set())


def make_is_enabled(category: str) -> Callable[[Any, Any], bool]:
    """Build an SDK is_enabled callback: visible only once this category is unlocked.

    The SDK calls it as (RunContextWrapper, AgentBase); both are ignored here —
    we read the ContextVar directly (this repo's isolation mechanism) and
    return a bool (sync is fine, the SDK accepts MaybeAwaitable[bool]).
    """
    def _is_enabled(_ctx: Any, _agent: Any) -> bool:
        return category in current_unlocked()
    return _is_enabled


# ---------------------------------------------------------------------------
# expand_tools meta-tool
# ---------------------------------------------------------------------------

from agents import function_tool
from skills import tool_registry as _reg


def _name(tool) -> str:
    return getattr(tool, "name", getattr(tool, "__name__", ""))


def categories_overview() -> str:
    lines = ["Unlockable tool categories (call expand_tools; unlocked tools appear on the next step):"]
    for cat, desc in _reg.CATEGORY_DESCRIPTIONS.items():
        lines.append(f"- {cat}: {desc}")
    return "\n".join(lines)


def _persist(categories: list[str]) -> None:
    """Persist the given list of unlocked categories (a standalone function so tests can monkeypatch it)."""
    import db
    session_id = GATING_SESSION_VAR.get("")
    if session_id:
        db.set_unlocked_categories(session_id, categories)


def _mcp_runtime_lines() -> list[str]:
    """Render the per-run MCP snapshot (same data as the system-prompt status
    line, at action-level detail). The static CATEGORY_TOOLS table only knows
    add_mcp_server — rendering it alone told the model no servers were
    connected (defect 2). On ANY failure fall back to a line that promises
    nothing rather than one that lies."""
    try:
        from mcp_client import status as _st
        return _st.render_expand_section(_st.MCP_STATUS_VAR.get())
    except Exception:
        return ["MCP runtime tools, if any, appear in your tool list on the next step."]


# expand_tools takes two different forms of input token:
#   "apps" / "mcp" / ...  -- a static category (a CATEGORY_TOOLS key)
#   "mcp:github"          -- a single MCP server's handle
#
# Why two levels: every MCP server used to share the single "mcp" gate, so
# opening it dumped N servers x M tools of full JSON schema into the prompt
# at once (roughly 20k tokens for a single 87-tool server).
# Now "mcp" only unlocks the management tools and returns a catalogue (tool
# names, no schema); only "mcp:github" loads that one server's FunctionTools
# into the live tool set.
def expand_categories(categories: list[str]) -> str:
    """Pure logic: unlock the given categories — including, for `mcp:<handle>`
    tokens, actually loading that server's real tools into the live run's
    Agent (L2) — and return the text shown to the model. Wrapped by
    expand_tools.

    The mcp:<handle> branch performs a blocking network fetch via
    asyncio.run() (see _load_l2_tools) rather than an inline `await`, because
    this function itself stays synchronous. That is safe here specifically
    because every caller of this function runs it off the thread that owns
    any already-running event loop: a direct call from a plain (non-async)
    test has no running loop at all, and expand_tools — the @function_tool
    wrapper the live model actually calls — explicitly hands this function to
    asyncio.to_thread (see below), which the SDK also does automatically for
    any *sync* function_tool. Either way, asyncio.run() here never collides
    with an already-running loop.
    """
    if not categories:
        return categories_overview()
    valid = set(_reg.CATEGORY_TOOLS.keys())
    handle_tokens = [c for c in categories if c not in valid and c.startswith("mcp:")]
    static_categories = [c for c in categories if c not in handle_tokens]

    unknown = [c for c in static_categories if c not in valid]
    if unknown:
        return (f"Unknown categories {unknown}. Valid categories: " + ", ".join(sorted(valid)) +
                ". Retry with these category names.")

    from skills import mcp_gating as _mcp   # local import: mcp_gating imports this module

    resolved: list[tuple[str, int]] = []   # (slug, server_id) for every valid mcp:<handle> token
    bad_handles: list[str] = []
    for token in handle_tokens:
        server_id = _mcp.resolve_handle(token)
        if server_id is None:
            bad_handles.append(token)
        else:
            slug = token.split(":", 1)[1] if ":" in token else token
            resolved.append((slug, server_id))
    if bad_handles:
        valid_handles = sorted(_mcp.MCP_HANDLES_VAR.get({}).keys())
        return (f"Unknown MCP handles {bad_handles}. Valid handles: " + ", ".join(valid_handles) +
                ". Retry with mcp:<handle>.")

    cur = current_unlocked()
    if not isinstance(cur, set):     # fallback: ensure it can be mutated in place
        cur = set(cur)
        UNLOCKED_VAR.set(cur)
    resolved_gate_keys = [_mcp.gate_key(sid) for _, sid in resolved]
    newly = [c for c in static_categories if c not in cur] + [k for k in resolved_gate_keys if k not in cur]
    # Only fetch/inject for a server whose gate THIS call is the one flipping —
    # a repeat expand_tools(["mcp:github"]) for an already-unlocked server must
    # not re-fetch or duplicate its tools in agent.tools.
    newly_resolved = [(slug, sid) for slug, sid in resolved if _mcp.gate_key(sid) not in cur]
    cur.update(static_categories)
    cur.update(resolved_gate_keys)
    _persist(sorted(cur))

    lines = [f"Unlocked: {', '.join(categories)}. The following tools are now available:"]
    for c in static_categories:
        for t in _reg.CATEGORY_TOOLS[c]:
            if c == "mcp":
                # label the admin tool and terminate with ";" so it reads as
                # one line among peers, not as the sole tool that "owns" the
                # server tool lists rendered below (doubao misread that layout
                # and kept calling the register tool instead of mcp__* tools)
                lines.append(f"System tool: {_name(t)};")
            else:
                lines.append(f"- {_name(t)}")
        if c == "mcp":
            lines.extend(_mcp_runtime_lines())

    # L2: fetch schemas and inject FunctionTools for every newly-opened server
    # gate, straight into the live run's agent.tools (see _load_l2_tools).
    # `loaded` maps slug -> count of tools actually added, for every slug this
    # call attempted; a slug that was already unlocked before this call (and
    # so wasn't attempted again) simply is not a key in it.
    loaded = _load_l2_tools(newly_resolved)
    for slug, _sid in resolved:
        if slug not in loaded:
            lines.append(f"- mcp:{slug}: already unlocked; its tools are already in your tool list")
        elif loaded[slug]:
            n = loaded[slug]
            lines.append(f"- mcp:{slug}: {n} tool{'s' if n != 1 else ''} loaded; "
                         "available in your tool list starting your next step")
        else:
            # fetch_schemas degraded to (0, []) — an untrusted/failed response
            # (network error, non-200, malformed body). The gate is open, but
            # there is nothing to show for it yet; tell the model plainly
            # rather than silently pretending nothing was requested.
            lines.append(f"- mcp:{slug}: gate opened, but no tool schemas could be loaded "
                         f"right now — try expand_tools([\"mcp:{slug}\"]) again shortly")

    if not newly:
        lines.append("(these categories were already unlocked — every tool named "
                      "above, with its full description, is ALREADY in your tool "
                      "list; call it directly by that exact name)")
    return "\n".join(lines)


def _load_l2_tools(pairs: list[tuple[str, int]]) -> dict[str, int]:
    """Synchronous entry point for L2 loading. See expand_categories's
    docstring for why asyncio.run() is safe to use here. Returns {slug:
    tools_added_count} for every (slug, server_id) pair in *pairs* — 0 means
    the fetch degraded to nothing, never that the pair was skipped."""
    if not pairs:
        return {}
    try:
        return asyncio.run(_load_l2_tools_async(pairs))
    except RuntimeError:
        # Defensive only: see expand_categories's docstring for why this
        # should be unreachable under the SDK's current threading model. If a
        # future change ever does call this from a thread that already owns a
        # running loop, degrade to "gate opened, nothing loaded yet" instead
        # of crashing the tool call.
        return {slug: 0 for slug, _ in pairs}


async def _load_l2_tools_async(pairs: list[tuple[str, int]]) -> dict[str, int]:
    """Fetch each server's full tool schemas and inject its FunctionTools into
    the live run's agent. Reads RUN_AGENT_VAR from mcp_client.client (imported
    here, not at module load time, to avoid a circular import: agent.py
    imports the skills package at startup) rather than from the `agent`
    module itself — agent.py just re-exports the same ContextVar object, but
    some tests reload "agent" out of sys.modules, which would otherwise risk
    reading a stale, disconnected copy; see that ContextVar's comment."""
    import mcp_client.client as _mc
    from mcp_client import runtime as _mcp_runtime

    counts: dict[str, int] = {}
    agent = _mc.RUN_AGENT_VAR.get(None)
    if agent is None:          # no run in progress (e.g. called outside a run)
        return {slug: 0 for slug, _ in pairs}

    servers_by_id = _mc._RUN_SERVERS_VAR.get(None) or {}
    write_token = _mc.WRITE_TOKEN_VAR.get("")
    new_tools = []
    for slug, server_id in pairs:
        # Fall back to a minimal stand-in when this run's server snapshot
        # doesn't have the id MCP_HANDLES_VAR resolved to (should not happen
        # in practice — both are derived from the same server list — but
        # this keeps the tool's NAME/identity correct even then; a real call
        # against it would fail with the normal "[MCP error] cannot connect"
        # message rather than the gate silently doing nothing).
        server = servers_by_id.get(server_id) or {"id": server_id, "name": slug}
        listed_at = server.get("listed_at", 0)
        entry = _mc._cache_get(server_id, listed_at)
        if entry is not None:
            schemas = entry.metas
        else:
            fetched_at, schemas = await _mcp_runtime.fetch_schemas(write_token, server_id)
            _mc._cache_put(server_id, schemas, fetched_at)
        n = 0
        for meta in schemas:
            if not isinstance(meta, dict) or not meta.get("name"):
                continue
            new_tools.append(_mc._wrap_tool(server, meta, slug=slug))
            n += 1
        counts[slug] = n

    if new_tools:
        # New list, never mutate in place: get_all_tools walks self.tools
        # every step; swapping the reference is free and side-steps any
        # question of mutating a list while it is being iterated.
        agent.tools = list(agent.tools) + new_tools
    return counts


@function_tool
async def expand_tools(categories: list[str]) -> str:
    """Unlock a set of tool categories, making their tools callable on the next step.

    At the start you only have a small set of core tools. When you need other
    capabilities, use this tool first to unlock the relevant categories — you
    can pass several at once, and should try to unlock all the categories this
    task is expected to need in one call.
    Passing an empty list returns an overview of all unlockable categories.

    Args:
        categories: List of category names to unlock, e.g. ["apps", "files"].
    """
    # expand_categories may block on real network I/O for mcp:<handle> tokens
    # (see its docstring), via its own internal asyncio.run() call. Running it
    # on a worker thread keeps this run's main event loop free while that
    # happens — and, just as importantly, guarantees expand_categories never
    # executes on the thread that owns an already-running loop, which is what
    # makes its asyncio.run() call safe (see its docstring).
    return await asyncio.to_thread(expand_categories, categories)
