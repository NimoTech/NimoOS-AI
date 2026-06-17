import pytest
import agent as agent_mod
import mcp_client.client as mc
from profiles import PROFILES


@pytest.mark.asyncio
async def test_build_called_and_closed(monkeypatch):
    """run() must build MCP tools from the passed servers and close them after."""
    closed = {"n": 0}

    class FakeConn:
        async def aclose(self): closed["n"] += 1

    async def fake_build(servers):
        return (["DUMMY_TOOL"], [FakeConn()] if servers else [])
    monkeypatch.setattr(mc, "build_mcp_tools", fake_build)

    tools, conns = await agent_mod._build_mcp_for_run([{"id": 1, "name": "x"}])
    assert tools == ["DUMMY_TOOL"]
    await agent_mod._close_mcp_conns(conns)
    assert closed["n"] == 1


@pytest.mark.asyncio
async def test_build_empty_is_safe(monkeypatch):
    tools, conns = await agent_mod._build_mcp_for_run([])
    assert tools == [] and conns == []
    await agent_mod._close_mcp_conns(conns)  # no error on empty


# ---------------------------------------------------------------------------
# §7.3 gate: MCP tools must only be added to the general (non-pinned) profile
# ---------------------------------------------------------------------------

def test_mcp_allowed_predicate_general():
    """general profile (tools=None) must allow MCP."""
    profile = PROFILES["general"]
    _mcp_allowed = profile is None or profile.tools is None
    assert _mcp_allowed, "general profile should allow MCP tools"


def test_mcp_allowed_predicate_photos():
    """photos profile (tools=tuple) must DENY MCP."""
    profile = PROFILES["photos"]
    _mcp_allowed = profile is None or profile.tools is None
    assert not _mcp_allowed, "photos (pinned) profile must not allow MCP tools"


@pytest.mark.asyncio
async def test_build_not_called_for_pinned_profile(monkeypatch):
    """When the profile is pinned, _build_mcp_for_run must not be called with
    real servers — it receives None and returns ([], []) without connecting."""
    build_mcp_calls = []

    async def fake_build(servers):
        build_mcp_calls.append(servers)
        # Should only be called with None for a pinned profile
        return ([], [])

    monkeypatch.setattr(mc, "build_mcp_tools", fake_build)

    pinned_profile = PROFILES["photos"]  # tools is a non-None tuple
    _mcp_allowed = pinned_profile is None or pinned_profile.tools is None

    # Simulate exactly what agent.py's run() now does:
    servers = [{"id": 1, "name": "test-server"}]
    tools, conns = await agent_mod._build_mcp_for_run(servers if _mcp_allowed else None)

    # build_mcp_tools was NOT called because _build_mcp_for_run short-circuits
    # on None before reaching build_mcp_tools
    assert tools == [], "pinned profile must produce no MCP tools"
    assert conns == [], "pinned profile must open no MCP connections"
    # build_mcp_tools itself was never invoked (None short-circuits in _build_mcp_for_run)
    assert build_mcp_calls == [], "build_mcp_tools must not be called for a pinned profile"


@pytest.mark.asyncio
async def test_build_called_for_general_profile(monkeypatch):
    """When the profile is general (tools=None), _build_mcp_for_run receives
    the real server list and calls build_mcp_tools."""
    build_mcp_calls = []

    async def fake_build(servers):
        build_mcp_calls.append(servers)
        return (["FAKE_TOOL"], [])

    monkeypatch.setattr(mc, "build_mcp_tools", fake_build)

    general_profile = PROFILES["general"]  # tools is None
    _mcp_allowed = general_profile is None or general_profile.tools is None

    servers = [{"id": 1, "name": "test-server"}]
    tools, conns = await agent_mod._build_mcp_for_run(servers if _mcp_allowed else None)

    assert tools == ["FAKE_TOOL"], "general profile must receive MCP tools"
    assert build_mcp_calls == [servers], "build_mcp_tools must be called for general profile"
