import pytest
import agent as agent_mod
import mcp_client.client as mc


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
