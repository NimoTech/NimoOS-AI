import asyncio
import sqlite3
import pytest

from confirm import ConfirmManager
import mcp_client.client as mc
from tests.conftest import unfence

META = {"name": "search", "description": "does a thing",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}}


class FakeQueue:
    def __init__(self): self.events = []
    async def put(self, e): self.events.append(e)


class FakeConn:
    def __init__(self): self.calls = []
    async def call_tool(self, name, args):
        self.calls.append((name, args))
        class Block: type = "text"; text = "RESULT"
        class Res: content = [Block()]; isError = False
        return Res()
    async def aclose(self): pass


def _setup(conn=None):
    sconn = sqlite3.connect(":memory:")
    sconn.execute("CREATE TABLE pending_confirmations (confirm_id TEXT, session_id TEXT, action TEXT, description TEXT, command TEXT, created_at INT)")
    mgr = ConfirmManager(sconn, timeout=5)
    q = FakeQueue()
    mc.SESSION_ID_VAR.set("s1")
    mc.EVENT_QUEUE_VAR.set(q)
    mc.CONFIRM_MGR_VAR.set(mgr)
    mc.USER_PATTERNS_VAR.set([])
    mc._CONFIRMED_TOOLS_VAR.set(set())
    mc._RUN_CONNS_VAR.set({1: conn} if conn else {})
    mc._RUN_CONN_LOCKS_VAR.set({})
    return mgr, q


def test_wrap_tool_name_and_schema():
    tool = mc._wrap_tool({"id": 1, "name": "My Git"}, META)
    assert tool.name == "mcp__my_git__search"
    assert tool.params_json_schema["properties"]["q"]["type"] == "string"
    assert tool.strict_json_schema is False


@pytest.mark.asyncio
async def test_invoke_confirm_then_call():
    fconn = FakeConn()
    mgr, q = _setup(conn=fconn)
    tool = mc._wrap_tool({"id": 1, "name": "git"}, META)

    async def approve():
        for _ in range(50):
            if q.events: break
            await asyncio.sleep(0.01)
        mgr.resolve(q.events[-1]["confirm_id"], confirmed=True, remember=False, expected_session_id="s1")

    asyncio.create_task(approve())
    out = await tool.on_invoke_tool(None, '{"q":"hi"}')
    # Third-party MCP results are external content — fenced as untrusted (L3).
    assert unfence(out, source="mcp-result") == "RESULT"
    assert fconn.calls == [("search", {"q": "hi"})]
    assert "1::search" not in mc._CONFIRMED_TOOLS_VAR.get(set())


@pytest.mark.asyncio
async def test_invoke_rejected_returns_text():
    mgr, q = _setup(conn=FakeConn())
    tool = mc._wrap_tool({"id": 1, "name": "git"}, META)

    async def reject():
        for _ in range(50):
            if q.events: break
            await asyncio.sleep(0.01)
        mgr.resolve(q.events[-1]["confirm_id"], confirmed=False, expected_session_id="s1")

    asyncio.create_task(reject())
    out = await tool.on_invoke_tool(None, '{"q":"hi"}')
    assert out.startswith("[MCP error]")
    assert "denied" in out


@pytest.mark.asyncio
async def test_remember_skips_second_confirm():
    fconn = FakeConn()
    mgr, q = _setup(conn=fconn)
    tool = mc._wrap_tool({"id": 1, "name": "git"}, META)

    async def approve_remember():
        for _ in range(50):
            if q.events: break
            await asyncio.sleep(0.01)
        mgr.resolve(q.events[-1]["confirm_id"], confirmed=True, remember=True, expected_session_id="s1")

    asyncio.create_task(approve_remember())
    await tool.on_invoke_tool(None, '{"q":"a"}')
    before = len(q.events)
    out = await tool.on_invoke_tool(None, '{"q":"b"}')
    assert unfence(out, source="mcp-result") == "RESULT"
    assert len(q.events) == before


@pytest.mark.asyncio
async def test_blacklist_blocks_path_arg():
    _setup(conn=FakeConn())
    mc.USER_PATTERNS_VAR.set(["/etc/"])
    tool = mc._wrap_tool({"id": 1, "name": "fs"},
                         {"name": "read", "description": "",
                          "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}})
    out = await tool.on_invoke_tool(None, '{"path":"/etc/shadow"}')
    assert out.startswith("[MCP error]")
    assert "blacklist" in out.lower()


@pytest.mark.asyncio
async def test_connect_failure_message_distinct(monkeypatch):
    mgr, q = _setup()                       # no pre-seeded conn
    async def boom(s): raise RuntimeError("net down")
    monkeypatch.setattr(mc, "_connect", boom)
    tool = mc._wrap_tool({"id": 1, "name": "git"}, META)

    async def approve():
        for _ in range(50):
            if q.events: break
            await asyncio.sleep(0.01)
        mgr.resolve(q.events[-1]["confirm_id"], confirmed=True, expected_session_id="s1")

    asyncio.create_task(approve())
    out = await tool.on_invoke_tool(None, '{"q":"hi"}')
    assert out.startswith("[MCP error]")
    assert "cannot connect" in out and "do NOT retry" in out
