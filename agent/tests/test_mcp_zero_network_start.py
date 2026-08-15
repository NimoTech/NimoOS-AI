import asyncio
from unittest.mock import patch, MagicMock

import pytest

import agent as ag
import mcp_client.client as mc


def test_run_start_opens_zero_connections(monkeypatch):
    """Direct assertion of requirement (1): no matter how many servers are
    configured or how healthy they are, run start must not open a single
    third-party connection."""
    called = []

    async def boom(*a, **kw):
        called.append(1)
        raise AssertionError("run start must NOT connect to any MCP server")

    monkeypatch.setattr(mc, "_connect", boom)
    servers = [{"id": 1, "name": "gh", "handle": "github", "listed_at": 0, "ttl_sec": 600,
                "tools": [{"name": "create_issue"}], "probe_state": "ok"}]
    tools, snapshot = asyncio.run(ag._build_mcp_for_run(servers))
    assert called == []
    assert snapshot is not None
    assert snapshot.servers[0].handle == "github"


def test_l2_appends_tools_to_the_live_agent(monkeypatch):
    """Design doc §0.3: the SDK re-reads agent.tools on every step
    (agents/run.py:1078 -> agents/agent.py:271), so replacing agent.tools
    mid-run takes effect on the very next step. This is the foundation L2
    rests on; if an SDK upgrade ever regresses that behavior, L2 silently
    breaks.

    NOTE on fake_fetch's signature: the brief's draft assumed
    `fetch_schemas(server_id)`, but Task 12 (see NimoOS-AI progress ledger,
    "Interface change Task 12 MUST know") already locked the real, tested
    signature as `fetch_schemas(write_token, server_id)` — the Go endpoint
    401s without the write token. Calling it with a single argument would
    raise TypeError against the REAL function too, not just skip auth, so
    this test's fake is written against the real two-argument signature
    rather than the brief's stale draft.
    """
    from agents import Agent
    from skills import tool_gating as tg, mcp_gating as mg

    a = Agent(name="t", instructions="i", tools=[])
    ag.RUN_AGENT_VAR.set(a)
    mg.MCP_HANDLES_VAR.set({"github": 7})
    tg.UNLOCKED_VAR.set(set())
    mc._SCHEMA_CACHE.clear()

    async def fake_fetch(write_token, server_id):
        return 100, [{"name": "create_issue", "description": "d", "input_schema": {"type": "object"}}]
    monkeypatch.setattr("mcp_client.runtime.fetch_schemas", fake_fetch)

    tg.expand_categories(["mcp:github"])
    names = [getattr(t, "name", "") for t in a.tools]
    assert any(n.startswith("mcp__github__") for n in names), \
        "L2 must inject this server's FunctionTools into the live agent.tools"


def test_l2_untrusted_zero_sentinel_injects_no_tools(monkeypatch):
    """Fix-2 regression pin: fetch_schemas degrading to (0, [...]) — a
    non-empty schemas array alongside the fetched_at==0 "never trust this"
    sentinel — must inject ZERO tools into the live agent and must tell the
    model something truthful, not silently splice in what would look like a
    working server's tools.

    Before this fix, _load_l2_tools_async cache-checked correctly (via
    _cache_put's own write guard) but then built FunctionTools from
    `schemas` unconditionally, ignoring the sentinel on the fetch path. This
    was latent only because Go never emitted listed_at == 0 alongside a
    non-empty tools array — a transport-relevant server Update now deletes
    the runtime AND schemas rows together (see route/v2/mcp.go's Update
    handler), making exactly this response shape reachable from a real,
    in-flight fetch immediately after such an edit.
    """
    from agents import Agent
    from skills import tool_gating as tg, mcp_gating as mg

    a = Agent(name="t", instructions="i", tools=[])
    ag.RUN_AGENT_VAR.set(a)
    mg.MCP_HANDLES_VAR.set({"github": 7})
    tg.UNLOCKED_VAR.set(set())
    mc._SCHEMA_CACHE.clear()

    async def fake_fetch(write_token, server_id):
        return 0, [{"name": "create_issue", "description": "d", "input_schema": {"type": "object"}}]
    monkeypatch.setattr("mcp_client.runtime.fetch_schemas", fake_fetch)

    out = tg.expand_categories(["mcp:github"])
    names = [getattr(t, "name", "") for t in a.tools]
    assert not any(n.startswith("mcp__github__") for n in names), \
        "an untrusted (0, [...]) fetch must never inject tools into the live agent"
    # Truthful reporting: neither silent success nor a fabricated tool count.
    assert "mcp:github: gate opened, but no tool schemas could be loaded" in out
    assert "1 tool loaded" not in out


def test_l2_reloads_when_the_persisted_gate_is_already_open(monkeypatch):
    """Review fix, C1: the unlock gate (UNLOCKED_VAR) is persisted per session
    and reloaded at the start of EVERY run/turn (agent.py's
    db.get_unlocked_categories), but agent.tools starts empty on every run.
    So on turn 2+ (or after continue_run, or a retry following a degraded
    fetch), the gate for a server can already be open while this run's fresh
    Agent has none of that server's tools yet. Before the fix,
    expand_categories used gate membership to decide whether to re-fetch and
    would report "already unlocked" while injecting nothing."""
    from agents import Agent
    from skills import tool_gating as tg, mcp_gating as mg

    a = Agent(name="t", instructions="i", tools=[])   # fresh agent, as every run starts
    ag.RUN_AGENT_VAR.set(a)
    mg.MCP_HANDLES_VAR.set({"github": 7})
    tg.UNLOCKED_VAR.set({mg.gate_key(7)})             # gate already open from a prior turn
    mc._SCHEMA_CACHE.clear()

    async def fake_fetch(write_token, server_id):
        return 101, [{"name": "create_issue", "description": "d", "input_schema": {"type": "object"}}]
    monkeypatch.setattr("mcp_client.runtime.fetch_schemas", fake_fetch)

    out = tg.expand_categories(["mcp:github"])
    names = [getattr(t, "name", "") for t in a.tools]
    assert any(n.startswith("mcp__github__") for n in names), \
        "an already-open gate must not suppress loading tools into a fresh agent.tools"
    # The per-server line (not the unrelated static-category "already
    # unlocked" footer, which legitimately still fires here since the GATE
    # really was already open) must report a real load, not a no-op.
    assert "mcp:github: 1 tool loaded" in out
    assert "mcp:github: already unlocked" not in out


@pytest.mark.asyncio
async def test_runtime_payload_sets_approvals_write_token_and_releases_it(monkeypatch, tmp_path):
    """Review fix, C2: main.py's /run endpoint fetches the full Runtime
    payload (approvals + a run-scoped write token, Task 8/12), and
    AgentRunner.run must thread both into the ContextVars _ensure_confirmed
    and the L2 schema fetch consume — then release the token at teardown."""
    from db import init_db
    from mcp_client.runtime import RuntimePayload

    conn = init_db(str(tmp_path / "c2.db"))
    conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) "
                 "VALUES('s-c2','u1',0,0)")
    conn.commit()
    runner = ag.AgentRunner(conn)

    seen = {}

    def fake_run_streamed(agent, input_messages, **kwargs):
        seen["confirmed"] = set(mc._CONFIRMED_TOOLS_VAR.get(set()))
        seen["write_token"] = mc.WRITE_TOKEN_VAR.get("")
        m = MagicMock()

        async def empty():
            return
            yield

        m.stream_events = empty
        m.to_input_list.return_value = []
        m.final_output = ""
        return m

    released = []

    async def fake_release(token):
        released.append(token)
    monkeypatch.setattr("mcp_client.runtime.release_token", fake_release)

    payload = RuntimePayload(servers=[], approvals={"1::create_issue"}, write_token="tok123")

    class _Sink:
        def __init__(self):
            self.events = []

        async def put(self, e):
            self.events.append(e)

    with patch("agent.Runner.run_streamed", side_effect=fake_run_streamed):
        await runner.run(session_id="s-c2", user_id="u1", message="hi", sink=_Sink(),
                         provider_key="k", provider_url="http://x", model_name="qwen",
                         mcp_servers=payload)

    assert seen["confirmed"] == {"1::create_issue"}, \
        "the run must consume Go's pre-filtered approval set, not always start empty"
    assert seen["write_token"] == "tok123", \
        "the run-scoped write token must reach _ensure_confirmed / the L2 schema fetch"
    assert released == ["tok123"], \
        "the write token must be released at run teardown, not left for the 24h backstop"


def test_expand_tools_does_not_duplicate_a_repeated_slug(monkeypatch):
    """Review fix: expand_tools(["mcp:github", "mcp:github"]) must not inject
    the same server's tools twice. The SDK does not dedupe FunctionTool names
    (agents/agent.py get_all_tools just concatenates), and OpenAI-compatible
    providers commonly reject a duplicate function name with a 400 — which
    would fail the WHOLE run, not just this expansion."""
    from agents import Agent
    from skills import tool_gating as tg, mcp_gating as mg

    a = Agent(name="t", instructions="i", tools=[])
    ag.RUN_AGENT_VAR.set(a)
    mg.MCP_HANDLES_VAR.set({"github": 7})
    tg.UNLOCKED_VAR.set(set())
    mc._SCHEMA_CACHE.clear()

    calls = {"n": 0}

    async def fake_fetch(write_token, server_id):
        calls["n"] += 1
        return 102, [{"name": "create_issue", "description": "d", "input_schema": {"type": "object"}}]
    monkeypatch.setattr("mcp_client.runtime.fetch_schemas", fake_fetch)

    tg.expand_categories(["mcp:github", "mcp:github"])
    names = [getattr(t, "name", "") for t in a.tools]
    assert names.count("mcp__github__create_issue") == 1, \
        "a repeated slug in one expand_tools call must not duplicate its tools"
    assert calls["n"] == 1, "the repeated slug must not trigger a second fetch either"


def test_concurrent_expand_of_the_same_server_does_not_duplicate(monkeypatch):
    """Review fix, the cross-thread half of the same finding: two
    expand_tools(["mcp:github"]) calls for the SAME server in one step run on
    two different worker threads (the SDK runs a step's tool calls in
    parallel tasks; expand_categories delegates to asyncio.to_thread). Both
    can pass the "already loaded" check against agent.tools before either
    has spliced in, independently fetch and build IDENTICAL FunctionTools,
    and then both try to add them — this pins that only one copy survives."""
    import contextvars
    import threading

    from agents import Agent
    from skills import tool_gating as tg, mcp_gating as mg

    a = Agent(name="t", instructions="i", tools=[])
    ag.RUN_AGENT_VAR.set(a)
    mg.MCP_HANDLES_VAR.set({"github": 7})
    tg.UNLOCKED_VAR.set(set())
    mc._SCHEMA_CACHE.clear()

    barrier = threading.Barrier(2)

    async def fake_fetch(write_token, server_id):
        # Force both threads to be mid-fetch (past the "already loaded"
        # check) simultaneously, so the merge race is actually exercised
        # instead of one call finishing before the other even starts.
        barrier.wait(timeout=5)
        return 103, [{"name": "create_issue", "description": "d", "input_schema": {"type": "object"}}]
    monkeypatch.setattr("mcp_client.runtime.fetch_schemas", fake_fetch)

    # Each thread needs its OWN ContextVar snapshot (RUN_AGENT_VAR etc.) —
    # this is what asyncio.to_thread does for a real concurrent expand_tools
    # call. A single Context object cannot be .run() from two threads at
    # once (CPython raises), so two independent copies are captured here,
    # mirroring two separate to_thread dispatches from the SAME ambient
    # context set above.
    ctx1 = contextvars.copy_context()
    ctx2 = contextvars.copy_context()
    results = []

    def worker(ctx):
        results.append(ctx.run(tg._load_l2_tools, [("github", 7)]))

    t1 = threading.Thread(target=worker, args=(ctx1,))
    t2 = threading.Thread(target=worker, args=(ctx2,))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    names = [getattr(t, "name", "") for t in a.tools]
    assert names.count("mcp__github__create_issue") == 1, \
        "two concurrent expansions of the same server must not duplicate its tools"


def test_sdk_still_reresolves_tools_each_turn():
    """Pin down both halves of §0.3's foundation:
    1. get_all_tools reads self.tools, an ordinary list attribute, not a
       snapshot taken once at Agent construction.
    2. The run loop calls get_all_tools INSIDE its per-turn while loop — both
       the streamed path (run_internal/run_loop.py's start_streaming) and the
       non-streamed path (run.py's AgentRunner.run) — not once before
       entering it. Without this half, an SDK change that hoisted tool
       resolution above the loop would leave get_all_tools's own
       self.tools-reading contract intact while silently breaking L2's
       "tools appear on the next step" guarantee, and this test would still
       report green.
    """
    import inspect
    from agents.agent import AgentBase
    from agents import run as _run_mod
    from agents.run_internal import run_loop as _run_loop_mod

    src = inspect.getsource(AgentBase.get_all_tools)
    assert "self.tools" in src, \
        "SDK no longer reads self.tools per turn — L2 progressive loading is broken"

    def _assert_inside_a_while_loop(fn, label):
        lines = inspect.getsource(fn).splitlines()
        call_idx = next(i for i, l in enumerate(lines)
                        if "get_all_tools(execution_agent" in l)
        call_indent = len(lines[call_idx]) - len(lines[call_idx].lstrip())
        # Walk upward for the nearest `while` whose indentation is STRICTLY
        # LESS than the call's — i.e. the call is nested inside that loop's
        # body, not merely textually below it in the same function.
        for i in range(call_idx - 1, -1, -1):
            stripped = lines[i].lstrip()
            indent = len(lines[i]) - len(stripped)
            if stripped.startswith("while") and indent < call_indent:
                return
        raise AssertionError(
            f"{label}: get_all_tools is no longer called inside a per-turn "
            "while loop — L2's 'tools appear on the next step' guarantee "
            "would silently break even though get_all_tools itself still "
            "reads self.tools")

    _assert_inside_a_while_loop(_run_mod.AgentRunner.run, "run.py (non-streamed)")
    _assert_inside_a_while_loop(_run_loop_mod.start_streaming, "run_internal/run_loop.py (streamed)")
