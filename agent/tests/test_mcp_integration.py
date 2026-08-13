import pytest
import agent as agent_mod
import mcp_client.client as mc
from mcp_client import status as st
from mcp_client.runtime import ConfigUnavailable


@pytest.mark.asyncio
async def test_build_returns_tools_and_snapshot(monkeypatch):
    async def fake_build(servers):
        return ["DUMMY_TOOL"], [st.ServerStatus(name="x", status=st.OK, tool_names=["t"])]
    monkeypatch.setattr(mc, "build_mcp_tools", fake_build)
    tools, snap = await agent_mod._build_mcp_for_run([{"id": 1, "name": "x"}])
    assert tools == ["DUMMY_TOOL"]
    assert snap.servers[0].name == "x" and snap.config_error == ""


@pytest.mark.asyncio
async def test_build_none_means_mcp_not_in_play():
    # pinned profiles / runs without a ticket path: no tools, no snapshot,
    # so no prompt line and the 2B fallback wording in expand_tools.
    assert await agent_mod._build_mcp_for_run(None) == ([], None)


@pytest.mark.asyncio
async def test_build_empty_list_is_a_no_servers_snapshot():
    tools, snap = await agent_mod._build_mcp_for_run([])
    assert tools == []
    assert snap is not None and snap.servers == [] and snap.config_error == ""


@pytest.mark.asyncio
async def test_build_config_unavailable_snapshot():
    tools, snap = await agent_mod._build_mcp_for_run(ConfigUnavailable("HTTP 500"))
    assert tools == []
    assert snap.config_error == "HTTP 500" and snap.servers == []


@pytest.mark.asyncio
async def test_build_never_raises(monkeypatch):
    async def boom(servers): raise RuntimeError("x")
    monkeypatch.setattr(mc, "build_mcp_tools", boom)
    assert await agent_mod._build_mcp_for_run([{"id": 1}]) == ([], None)


def test_apply_mcp_status_appends_line_and_publishes_var():
    snap = st.McpStatusSnapshot(servers=[
        st.ServerStatus(name="g", status=st.OK, tool_names=["a", "b"])])
    out = agent_mod._apply_mcp_status("BASE", snap)
    assert out.startswith("BASE\n\n[MCP servers: ")
    assert "g: 2 tools ready" in out
    assert st.MCP_STATUS_VAR.get() is snap      # expand_tools reads the same snapshot


def test_apply_mcp_status_none_is_a_noop_line():
    assert agent_mod._apply_mcp_status("BASE", None) == "BASE"
    assert st.MCP_STATUS_VAR.get() is None


def test_apply_mcp_status_never_raises(monkeypatch):
    monkeypatch.setattr(st, "render_prompt_line",
                        lambda snap: (_ for _ in ()).throw(RuntimeError("render broke")))
    assert agent_mod._apply_mcp_status("BASE", st.McpStatusSnapshot()) == "BASE"
