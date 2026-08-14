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
    async def list_tools(self):
        return [{"name": "search", "description": "does a thing",
                 "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}}], mc.SCHEMA_TTL
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


# --- defect ①: unknown-tool errors must invalidate the schema cache ---

from mcp.shared.exceptions import MCPError
from mcp.types import INTERNAL_ERROR, METHOD_NOT_FOUND


def test_is_unknown_tool_signatures():
    assert mc._is_unknown_tool(MCPError(code=METHOD_NOT_FOUND, message="x")) is True
    assert mc._is_unknown_tool(MCPError(code=INTERNAL_ERROR, message="Unknown tool: search")) is True
    assert mc._is_unknown_tool(MCPError(code=INTERNAL_ERROR, message="Tool 'search' not found")) is True
    assert mc._is_unknown_tool(MCPError(code=INTERNAL_ERROR, message="bad argument foo")) is False
    assert mc._is_unknown_tool(RuntimeError("Unknown tool")) is False   # not an MCPError shape
    # resource errors that merely mention a tool name must NOT match — dropping
    # the cache here would tell the model not to retry a recoverable error
    assert mc._is_unknown_tool(MCPError(
        code=INTERNAL_ERROR,
        message="Error executing tool read_file: File not found")) is False
    assert mc._is_unknown_tool(MCPError(code=INTERNAL_ERROR, message="No such tool: search")) is True


def _approve_first(mgr, q):
    async def approve():
        for _ in range(50):
            if q.events: break
            await asyncio.sleep(0.01)
        mgr.resolve(q.events[-1]["confirm_id"], confirmed=True, remember=False,
                    expected_session_id="s1")
    return approve


@pytest.mark.asyncio
async def test_unknown_tool_drops_cache_and_schedules_refresh(monkeypatch):
    class UnknownToolConn:
        async def call_tool(self, name, args):
            raise MCPError(code=METHOD_NOT_FOUND, message="Unknown tool: search")
        async def aclose(self): pass

    mgr, q = _setup(conn=UnknownToolConn())
    mc._SCHEMA_CACHE.clear()
    mc._cache_put(1, [META], listed_at=1)
    scheduled = []
    monkeypatch.setattr(mc, "_schedule_revalidate", lambda s: scheduled.append(s["id"]))
    tool = mc._wrap_tool({"id": 1, "name": "git"}, META)

    asyncio.create_task(_approve_first(mgr, q)())
    out = await tool.on_invoke_tool(None, '{"q":"hi"}')
    assert "no longer recognizes" in out and "do NOT" in out
    assert "next message" in out                # the run's tool set is immutable (SDK decision) — the message states that honestly
    assert 1 not in mc._SCHEMA_CACHE            # stale entry dropped
    assert scheduled == [1]                     # refresh scheduled for the next run


@pytest.mark.asyncio
async def test_ordinary_mcp_error_keeps_cache(monkeypatch):
    # Invalidating on ANY MCPError would let plain argument errors punch
    # through the warm path this cache exists to provide.
    class ArgErrorConn:
        async def call_tool(self, name, args):
            raise MCPError(code=INTERNAL_ERROR, message="invalid value for argument q")
        async def aclose(self): pass

    mgr, q = _setup(conn=ArgErrorConn())
    mc._SCHEMA_CACHE.clear()
    mc._cache_put(1, [META], listed_at=1)
    scheduled = []
    monkeypatch.setattr(mc, "_schedule_revalidate", lambda s: scheduled.append(s["id"]))
    tool = mc._wrap_tool({"id": 1, "name": "git"}, META)

    asyncio.create_task(_approve_first(mgr, q)())
    out = await tool.on_invoke_tool(None, '{"q":"hi"}')
    assert out.startswith("[MCP error] MCP tool search failed")
    assert 1 in mc._SCHEMA_CACHE                # cache untouched
    assert scheduled == []
