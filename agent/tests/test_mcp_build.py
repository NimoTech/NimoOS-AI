import pytest
import mcp_client.client as mc


class GoodSrv:
    def __init__(self): self.closed = False
    async def connect(self): pass
    async def list_tools(self):
        class T:
            name = "search"; description = "d"
            inputSchema = {"type": "object", "properties": {}}
        return [T()]
    async def call_tool(self, name, args): ...
    async def cleanup(self): self.closed = True


@pytest.mark.asyncio
async def test_build_skips_failing_server(monkeypatch):
    events = []
    async def fake_emit(name, err): events.append((name, str(err)))
    monkeypatch.setattr(mc, "_emit_warning", fake_emit)

    async def fake_connect(s):
        if s["name"] == "bad":
            raise RuntimeError("boom")
        conn = mc.McpConn(server=s, srv=GoodSrv())
        return conn
    monkeypatch.setattr(mc, "_connect", fake_connect)

    servers = [{"id": 1, "name": "good"}, {"id": 2, "name": "bad"}]
    tools, conns = await mc.build_mcp_tools(servers)
    assert len(tools) == 1
    assert tools[0].name == "mcp__good__search"
    assert len(conns) == 1
    assert any(n == "bad" for n, _ in events)


@pytest.mark.asyncio
async def test_build_handles_list_tools_failure(monkeypatch):
    events = []
    async def fake_emit(name, err): events.append(name)
    monkeypatch.setattr(mc, "_emit_warning", fake_emit)

    class BadList(GoodSrv):
        async def list_tools(self): raise RuntimeError("list boom")

    async def fake_connect(s):
        return mc.McpConn(server=s, srv=BadList())
    monkeypatch.setattr(mc, "_connect", fake_connect)

    tools, conns = await mc.build_mcp_tools([{"id": 1, "name": "x"}])
    assert tools == []
    assert conns == []
    assert events == ["x"]


@pytest.mark.asyncio
async def test_build_dedupes_colliding_tool_names(monkeypatch):
    """Two servers whose slug+toolname collide must not produce duplicate
    FunctionTool names (silent shadowing). The second is disambiguated."""
    async def fake_emit(name, err): pass
    monkeypatch.setattr(mc, "_emit_warning", fake_emit)

    async def fake_connect(s):
        return mc.McpConn(server=s, srv=GoodSrv())
    monkeypatch.setattr(mc, "_connect", fake_connect)

    # "My Git" and "my-git" both slug to "my_git"
    servers = [{"id": 1, "name": "My Git"}, {"id": 2, "name": "my-git"}]
    tools, conns = await mc.build_mcp_tools(servers)
    names = [t.name for t in tools]
    assert len(names) == len(set(names)), f"duplicate tool names: {names}"
    assert len(conns) == 2
