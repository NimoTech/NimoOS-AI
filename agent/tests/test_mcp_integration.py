import pytest
import agent as agent_mod
import mcp_client.client as mc


@pytest.mark.asyncio
async def test_build_returns_list(monkeypatch):
    async def fake_build(servers):
        return ["DUMMY_TOOL"] if servers else []
    monkeypatch.setattr(mc, "build_mcp_tools", fake_build)
    assert await agent_mod._build_mcp_for_run([{"id": 1, "name": "x"}]) == ["DUMMY_TOOL"]


@pytest.mark.asyncio
async def test_build_empty_is_safe():
    assert await agent_mod._build_mcp_for_run([]) == []
    assert await agent_mod._build_mcp_for_run(None) == []


@pytest.mark.asyncio
async def test_build_never_raises(monkeypatch):
    async def boom(servers): raise RuntimeError("x")
    monkeypatch.setattr(mc, "build_mcp_tools", boom)
    assert await agent_mod._build_mcp_for_run([{"id": 1}]) == []
