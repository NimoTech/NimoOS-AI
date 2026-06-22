import json
from unittest.mock import MagicMock

import pytest

import skills.mcp_admin as ma
import mcp_client.client as mc
import skills.skills_registry as sr
from confirm import ConfirmManager


def _make_mgr():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE pending_confirmations "
        "(confirm_id TEXT, session_id TEXT, action TEXT, "
        "description TEXT, command TEXT, created_at INT)"
    )
    return ConfirmManager(conn, timeout=5)


def _setup(monkeypatch, *, parse_ret, approve, register_ret=None):
    monkeypatch.setattr(ma, "_read_ai_base", lambda: "http://127.0.0.1:9")
    async def fake_parse(base, cmd):
        return parse_ret
    async def fake_register(base, uid, cmd, name):
        fake_register.called = (base, uid, cmd, name)
        return register_ret or {"id": 1, "name": parse_ret.get("suggested_name", "x"),
                                "transport": parse_ret.get("transport", "stdio")}
    fake_register.called = None
    monkeypatch.setattr(ma, "_parse", fake_parse)
    monkeypatch.setattr(ma, "_register", fake_register)
    mgr = _make_mgr()
    import asyncio
    q = asyncio.Queue()
    mc.CONFIRM_MGR_VAR.set(mgr)
    mc.EVENT_QUEUE_VAR.set(q)
    mc.SESSION_ID_VAR.set("sess1")
    sr.USER_ID_VAR.set("7")
    async def auto():
        ev = await q.get()
        mgr.resolve(ev["confirm_id"], approve, remember=False)
    return mgr, q, auto, fake_register


@pytest.mark.asyncio
async def test_register_approved(monkeypatch):
    import asyncio
    mgr, q, auto, fake_register = _setup(monkeypatch, approve=True, parse_ret={
        "transport": "stdio", "command": "npx", "args": ["-y", "@pkg"],
        "env": {}, "url": "", "suggested_name": "pkg"})
    task = asyncio.create_task(auto())
    out = await ma.mcp_register_server.on_invoke_tool(
        MagicMock(), json.dumps({"command_line": "npx -y @pkg"}))
    await task
    assert "已注册" in out
    assert fake_register.called[1] == "7"
    assert fake_register.called[2] == "npx -y @pkg"
    assert fake_register.called[3] == "pkg"  # display_name = suggested_name when name omitted


@pytest.mark.asyncio
async def test_register_denied(monkeypatch):
    import asyncio
    mgr, q, auto, fake_register = _setup(monkeypatch, approve=False, parse_ret={
        "transport": "stdio", "command": "npx", "args": [], "env": {}, "url": "",
        "suggested_name": "pkg"})
    task = asyncio.create_task(auto())
    out = await ma.mcp_register_server.on_invoke_tool(
        MagicMock(), json.dumps({"command_line": "npx -y @pkg"}))
    await task
    assert "拒绝" in out
    assert fake_register.called is None


@pytest.mark.asyncio
async def test_register_parse_error(monkeypatch):
    monkeypatch.setattr(ma, "_read_ai_base", lambda: "http://127.0.0.1:9")
    async def boom_parse(base, cmd):
        raise ma.ParseError("empty command")
    monkeypatch.setattr(ma, "_parse", boom_parse)
    out = await ma.mcp_register_server.on_invoke_tool(
        MagicMock(), json.dumps({"command_line": "   "}))
    assert "解析失败" in out


@pytest.mark.asyncio
async def test_register_no_ai_base(monkeypatch):
    monkeypatch.setattr(ma, "_read_ai_base", lambda: None)
    out = await ma.mcp_register_server.on_invoke_tool(
        MagicMock(), json.dumps({"command_line": "npx -y @pkg"}))
    assert "无法" in out or "系统错误" in out
