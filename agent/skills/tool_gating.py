"""Runtime gating primitives for progressive tool exposure.

The unlocked set lives in a ContextVar (same concurrency-isolation pattern as
the other *_VAR globals in agent.py): each run does UNLOCKED_VAR.set(<set
loaded from the session>) at the start, expand_tools updates it in place, and
the is_enabled callback of non-always-on tools reads it to decide whether
they're visible to the model.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from contextvars import ContextVar
from typing import Any, Callable

_LOG = logging.getLogger("nimoos-agent")

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
    seen_slugs: set[str] = set()           # dedup e.g. expand_tools(["mcp:github","mcp:github"])
    bad_handles: list[str] = []
    for token in handle_tokens:
        server_id = _mcp.resolve_handle(token)
        if server_id is None:
            bad_handles.append(token)
        else:
            slug = token.split(":", 1)[1] if ":" in token else token
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)
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

    # L2: fetch schemas and inject FunctionTools for every requested server —
    # for ALL of `resolved`, not just the ones whose GATE this call happens to
    # be flipping. The persisted unlock gate (`cur`, written above) survives
    # across runs and turns (agent.py reloads it from the DB at every run
    # start via db.get_unlocked_categories), but agent.tools starts EMPTY on
    # every run (see _build_mcp_for_run) — so gate membership can never tell
    # us whether THIS run's live agent actually has the tools yet. Whether to
    # skip re-fetching is decided inside _load_l2_tools_async instead, from
    # the live agent.tools contents. `loaded` maps slug -> either None (tools
    # were already present in the live agent, nothing fetched) or an int
    # count of tools actually added by this call (0 = fetched but empty).
    loaded = _load_l2_tools(resolved)
    for slug, _sid in resolved:
        if loaded.get(slug) is None:
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


# Serializes the read-modify-write of agent.tools across CONCURRENT
# expand_tools calls. The SDK runs the model's tool calls for one step in
# parallel tasks; since expand_categories now runs on a worker thread (see
# expand_tools below), two expand_tools calls in the same step — for
# different servers, or (a race, see _load_l2_tools_async) even the SAME
# server — can otherwise race on splicing their built tools into
# agent.tools from two different threads. The lock guards a fresh
# read-check-append-assign each time a server's tools are ready to splice in
# (not the whole function), and does NOT hold across the network fetch that
# precedes it, so concurrent servers still fetch in parallel.
_TOOLS_MERGE_LOCK = threading.Lock()


def _load_l2_tools(pairs: list[tuple[str, int]]) -> dict[str, "int | None"]:
    """Synchronous entry point for L2 loading. See expand_categories's
    docstring for why asyncio.run() is safe to use here — that safety
    argument covers ONLY the "no running loop in this thread" precondition
    asyncio.run() itself requires, so it is checked explicitly up front
    rather than papered over with a broad try/except: a RuntimeError raised
    from INSIDE _load_l2_tools_async (a real bug) must propagate and be
    visible, not be silently reported as "0 tools loaded".

    Returns {slug: tools_added_count | None} for every (slug, server_id) pair
    in *pairs* — None means the tools were already present in the live
    agent.tools (nothing fetched); 0 means a fetch was attempted and
    degraded to nothing; neither means the pair was silently skipped.
    """
    if not pairs:
        return {}
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass    # expected: see expand_categories's docstring — no loop here
    else:
        # Defensive only: should be unreachable under the SDK's current
        # threading model (expand_tools always hands this to a worker
        # thread). Log loudly rather than crash the tool call outright if a
        # future change ever does call this from a thread that already owns
        # a running loop.
        _LOG.warning(
            "_load_l2_tools called from a thread with an already-running "
            "event loop; skipping L2 injection this call instead of "
            "crashing on asyncio.run() (pairs=%r)", pairs)
        return {slug: 0 for slug, _ in pairs}
    return asyncio.run(_load_l2_tools_async(pairs))


async def _fetch_and_build(slug: str, server_id: int) -> list:
    """Fetch one server's tool schemas (cache-first) and build its FunctionTools.

    The ONLY place this repo turns a (slug, server_id) pair into live
    FunctionTools. Both callers go through here on purpose:
      - _load_l2_tools_async — mid-run expand_tools(["mcp:<slug>"]);
      - rehydrate_unlocked_mcp_tools — run start, for gates already open.
    A second, parallel implementation is exactly how the deleted
    build_mcp_tools and its inline replacement drifted apart on the
    `listed_at == 0` trust sentinel (see AS-BUILT §2.3): one honoured it, the
    other built tools from a degraded response. Keep it single.

    Delegates cache-check + fetch + that sentinel to _metas_for_server, which
    also emits the UI warning event on failure. Returns [] when the fetch
    degraded to (0, []) — never a half-trusted tool list.
    """
    import mcp_client.client as _mc

    servers_by_id = _mc._RUN_SERVERS_VAR.get(None) or {}
    # Fall back to a minimal stand-in when this run's server snapshot doesn't
    # have the id MCP_HANDLES_VAR resolved to (should not happen in practice —
    # both are derived from the same server list — but this keeps the tool's
    # NAME/identity correct even then; a real call against it would fail with
    # the normal "[MCP error] cannot connect" message rather than the gate
    # silently doing nothing).
    server = servers_by_id.get(server_id) or {"id": server_id, "name": slug}
    schemas, _status, _detail = await _mc._metas_for_server(server)
    built = []
    for meta in schemas:
        if not isinstance(meta, dict) or not meta.get("name"):
            continue
        built.append(_mc._wrap_tool(server, meta, slug=slug))
    return built


async def rehydrate_unlocked_mcp_tools() -> tuple[list, set[str]]:
    """Rebuild the FunctionTools of every MCP server whose gate is ALREADY open
    for this session, for inclusion in this run's initial tool list.

    Why this exists: the unlock gate is session-scoped (persisted in
    sessions.unlocked_tool_categories, reloaded into UNLOCKED_VAR at every run
    start) but the tools it authorizes were only ever run-scoped — mid-run
    expand_tools splices them into the live Agent object, which is discarded
    when the run ends. So from the SECOND user message onward the model was
    told (by the L0 prompt line, by the persisted gate, and by its own history)
    that a server's tools were loaded while the request's tool array no longer
    contained them; calling one raised ModelBehaviorError "Tool
    mcp__<slug>__<tool> not found in agent". This closes that gap by making the
    persisted gate actually load-bearing at run start.

    Costs no third-party network: _fetch_and_build -> _metas_for_server reads
    the process-level _SCHEMA_CACHE first (which spans runs) and otherwise asks
    Go over loopback. Servers whose gate is closed are never even looked at, so
    a fresh session's first turn does exactly zero work here.

    Returns (tools, loaded_slugs) — loaded_slugs feeds the L0/L1 wording so it
    can say "already in your tool list" for these and "call expand_tools" for
    the rest. Never raises: MCP is additive and must not stop a run from
    starting.
    """
    from skills import mcp_gating as _mcp   # local import: mcp_gating imports this module
    import mcp_client.client as _mc

    unlocked = current_unlocked()
    handles = _mcp.MCP_HANDLES_VAR.get({}) or {}
    servers_by_id = _mc._RUN_SERVERS_VAR.get(None) or {}
    tools: list = []
    loaded: set[str] = set()
    # Iterating this run's handles (not the gate keys) means a gate whose
    # server is gone from this run's list — deleted, or disabled in settings —
    # is skipped silently. The key is deliberately NOT pruned from the
    # persisted set on absence: disabling and re-enabling a server must not
    # revoke anything (the rev-1 defect this branch exists to keep fixed), and
    # "deleted" is indistinguishable from "temporarily disabled" here.
    for slug, server_id in sorted(handles.items()):
        if _mcp.gate_key(server_id) not in unlocked:
            continue
        # Never advertise a server Go flagged as having undecryptable
        # credentials as connectable — same rule _build_mcp_for_run applies to
        # the status snapshot.
        if (servers_by_id.get(server_id) or {}).get("config_error"):
            continue
        try:
            built = await _fetch_and_build(slug, server_id)
        except Exception:
            _LOG.warning("MCP rehydration failed for %r (server_id=%s); "
                         "continuing without its tools", slug, server_id,
                         exc_info=True)
            continue
        if built:
            tools.extend(built)
            loaded.add(slug)
    return tools, loaded


async def _load_l2_tools_async(pairs: list[tuple[str, int]]) -> dict[str, "int | None"]:
    """Fetch each server's full tool schemas and inject its FunctionTools into
    the live run's agent. Reads RUN_AGENT_VAR from mcp_client.client (imported
    here, not at module load time, to avoid a circular import: agent.py
    imports the skills package at startup) rather than from the `agent`
    module itself — agent.py just re-exports the same ContextVar object, but
    some tests reload "agent" out of sys.modules, which would otherwise risk
    reading a stale, disconnected copy; see that ContextVar's comment."""
    import mcp_client.client as _mc

    counts: dict[str, "int | None"] = {}
    agent = _mc.RUN_AGENT_VAR.get(None)
    if agent is None:          # no run in progress (e.g. called outside a run)
        return {slug: 0 for slug, _ in pairs}

    for slug, server_id in pairs:
        if slug in counts:
            # Defense in depth against a duplicate (slug, server_id) pair
            # WITHIN this same call — expand_categories already dedupes
            # `resolved` before calling in, so this should be unreachable in
            # practice, but a direct/future caller of this function with
            # duplicate pairs must not fetch or build the same server twice.
            continue
        prefix = f"mcp__{slug}__"
        # Re-derive "already loaded" from the LIVE agent's tool list, never
        # from the persisted unlock gate: the gate survives across runs/turns
        # (agent.py reloads it from the DB at every run start) while
        # agent.tools starts empty on every run, so gate membership alone
        # cannot tell us whether THIS run's agent has the tools yet. Skipping
        # here also makes a retry after a degraded (0, []) fetch actually
        # retry, instead of forever reporting stale "already unlocked" text.
        if any(getattr(t, "name", "").startswith(prefix) for t in agent.tools):
            counts[slug] = None
            continue
        # Shared with run-start rehydration — see _fetch_and_build for why the
        # fetch + trust-sentinel + build sequence lives in exactly one place.
        built = await _fetch_and_build(slug, server_id)

        if built:
            # New list, never mutate in place: get_all_tools walks self.tools
            # every step; swapping the reference is free and side-steps any
            # question of mutating a list while it is being iterated.
            #
            # Locked AND re-checked against the CURRENT agent.tools right
            # here, not just the "already loaded" check above: two
            # concurrent expand_tools calls naming the SAME server (the SDK
            # runs one step's tool calls in parallel tasks, and
            # expand_categories runs each on its own worker thread) can both
            # pass that check before either has spliced in, independently
            # fetch and build IDENTICAL FunctionTools, and then both try to
            # add them — the SDK does not dedupe by name, and most
            # OpenAI-compatible providers reject a duplicate function name
            # with a 400, failing the whole run. Filtering `built` against a
            # FRESH read of agent.tools while holding the lock means whichever
            # thread splices first wins and the second contributes nothing
            # extra, instead of both adding the same names.
            with _TOOLS_MERGE_LOCK:
                existing_names = {getattr(t, "name", "") for t in agent.tools}
                to_add = [t for t in built if getattr(t, "name", "") not in existing_names]
                if to_add:
                    agent.tools = list(agent.tools) + to_add
        # Report the count of schemas this server actually returned, not
        # len(to_add): if a concurrent call already spliced these exact
        # tools in first, the true state is still "this server has len(built)
        # tools available" — reporting 0 here would be misleading even
        # though THIS call's splice contributed nothing.
        counts[slug] = len(built)

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
