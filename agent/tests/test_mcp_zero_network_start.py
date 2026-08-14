import asyncio
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


def test_sdk_still_reresolves_tools_each_turn():
    """Pin down §0.3's foundation: get_all_tools reads self.tools, an
    ordinary list attribute, not a snapshot taken once at Agent construction."""
    import inspect
    from agents.agent import AgentBase
    src = inspect.getsource(AgentBase.get_all_tools)
    assert "self.tools" in src, \
        "SDK no longer reads self.tools per turn — L2 progressive loading is broken"
