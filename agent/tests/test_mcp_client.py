import asyncio
import sqlite3
import pytest

from confirm import ConfirmManager
import mcp_client.client as mc


class FakeQueue:
    def __init__(self): self.events = []
    async def put(self, e): self.events.append(e)


class FakeMcpTool:
    def __init__(self, name, schema=None):
        self.name = name
        self.description = "does a thing"
        self.inputSchema = schema or {"type": "object", "properties": {"q": {"type": "string"}}}


class FakeConn:
    def __init__(self): self.calls = []
    async def call_tool(self, name, args):
        self.calls.append((name, args))
        class Block: type = "text"; text = "RESULT"
        class Res: content = [Block()]; isError = False
        return Res()


def _setup():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE pending_confirmations (confirm_id TEXT, session_id TEXT, action TEXT, description TEXT, command TEXT, created_at INT)")
    mgr = ConfirmManager(conn, timeout=5)
    q = FakeQueue()
    mc.SESSION_ID_VAR.set("s1")
    mc.EVENT_QUEUE_VAR.set(q)
    mc.CONFIRM_MGR_VAR.set(mgr)
    mc.USER_PATTERNS_VAR.set([])
    mc._CONFIRMED_TOOLS_VAR.set(set())
    return mgr, q


def test_wrap_tool_name_and_schema():
    server = {"id": 1, "name": "My Git"}
    tool = mc._wrap_tool(server, FakeConn(), FakeMcpTool("search"))
    assert tool.name == "mcp__my_git__search"
    assert tool.params_json_schema["properties"]["q"]["type"] == "string"
    assert tool.strict_json_schema is False


@pytest.mark.asyncio
async def test_invoke_confirm_then_call():
    mgr, q = _setup()
    server = {"id": 1, "name": "git"}
    fconn = FakeConn()
    tool = mc._wrap_tool(server, fconn, FakeMcpTool("search"))

    async def approve():
        for _ in range(50):
            if q.events:
                break
            await asyncio.sleep(0.01)
        cid = q.events[-1]["confirm_id"]
        mgr.resolve(cid, confirmed=True, remember=False, expected_session_id="s1")

    asyncio.create_task(approve())
    out = await tool.on_invoke_tool(None, '{"q":"hi"}')
    assert out == "RESULT"
    assert fconn.calls == [("search", {"q": "hi"})]
    assert "1::search" not in mc._CONFIRMED_TOOLS_VAR.get(set())


@pytest.mark.asyncio
async def test_invoke_rejected_returns_text():
    mgr, q = _setup()
    tool = mc._wrap_tool({"id": 1, "name": "git"}, FakeConn(), FakeMcpTool("search"))

    async def reject():
        for _ in range(50):
            if q.events:
                break
            await asyncio.sleep(0.01)
        mgr.resolve(q.events[-1]["confirm_id"], confirmed=False, expected_session_id="s1")

    asyncio.create_task(reject())
    out = await tool.on_invoke_tool(None, '{"q":"hi"}')
    assert "拒绝" in out


@pytest.mark.asyncio
async def test_remember_skips_second_confirm():
    mgr, q = _setup()
    fconn = FakeConn()
    tool = mc._wrap_tool({"id": 1, "name": "git"}, fconn, FakeMcpTool("search"))

    async def approve_remember():
        for _ in range(50):
            if q.events:
                break
            await asyncio.sleep(0.01)
        mgr.resolve(q.events[-1]["confirm_id"], confirmed=True, remember=True, expected_session_id="s1")

    asyncio.create_task(approve_remember())
    await tool.on_invoke_tool(None, '{"q":"a"}')
    before = len(q.events)
    out = await tool.on_invoke_tool(None, '{"q":"b"}')
    assert out == "RESULT"
    assert len(q.events) == before


@pytest.mark.asyncio
async def test_blacklist_blocks_path_arg():
    _setup()
    mc.USER_PATTERNS_VAR.set(["/etc/"])
    tool = mc._wrap_tool({"id": 1, "name": "fs"}, FakeConn(),
                         FakeMcpTool("read", {"type": "object", "properties": {"path": {"type": "string"}}}))
    out = await tool.on_invoke_tool(None, '{"path":"/etc/shadow"}')
    assert "黑名单" in out or "blacklist" in out.lower()
