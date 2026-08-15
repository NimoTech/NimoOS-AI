import pytest
import agent as agent_mod
import mcp_client.client as mc
from mcp_client import status as st
from mcp_client.runtime import ConfigUnavailable


@pytest.mark.asyncio
async def test_build_returns_snapshot_without_connecting(monkeypatch):
    """Task 16: _build_mcp_for_run is pure construction from the server dicts
    Go already handed down at run start — it must NEVER connect to any
    server (build_mcp_tools, which used to do that, was deleted once L2
    stopped calling it — see skills/tool_gating.py's _load_l2_tools_async,
    which now routes through _metas_for_server directly). Run start's tool
    list is always empty; L2 loads a server's real tools later."""
    async def boom(*a, **kw):
        raise AssertionError("_build_mcp_for_run must not connect to any MCP server")
    monkeypatch.setattr(mc, "_connect", boom)
    tools, snap = await agent_mod._build_mcp_for_run(
        [{"id": 1, "name": "x", "handle": "x", "probe_state": "ok",
          "tools": [{"name": "t"}]}])
    assert tools == []
    assert snap.servers[0].name == "x" and snap.config_error == ""
    assert snap.servers[0].status == st.OK
    assert snap.servers[0].handle == "x"
    assert snap.servers[0].tool_names == ["mcp__x__t"]


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
    def boom(servers): raise RuntimeError("x")
    monkeypatch.setattr(mc, "assign_slugs", boom)
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
