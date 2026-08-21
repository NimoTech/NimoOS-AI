import sys, pathlib, asyncio
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import pytest
from unittest.mock import MagicMock


class FakeMgr:
    def __init__(self, answer): self.answer = answer
    def register(self, *a, **k): return "cid-1"
    async def wait(self, cid): return self.answer


@pytest.mark.asyncio
async def test_install_component_confirmed(monkeypatch):
    from skills import toolbox_admin
    from mcp_client import client as mc
    calls = []

    async def fake_install(conn, cid): calls.append(cid)
    monkeypatch.setattr(toolbox_admin, "_do_install", fake_install)
    t1 = mc.CONFIRM_MGR_VAR.set(FakeMgr(True))
    t2 = mc.EVENT_QUEUE_VAR.set(asyncio.Queue())
    t3 = mc.SESSION_ID_VAR.set("s1")
    try:
        out = await toolbox_admin.install_component.on_invoke_tool(MagicMock(), '{"component_id": "gh"}')
    finally:
        mc.CONFIRM_MGR_VAR.reset(t1); mc.EVENT_QUEUE_VAR.reset(t2); mc.SESSION_ID_VAR.reset(t3)
    assert calls == ["gh"] and "installed" in out.lower()


@pytest.mark.asyncio
async def test_install_component_denied(monkeypatch):
    from skills import toolbox_admin
    from mcp_client import client as mc
    called = []

    async def fake_install(conn, cid): called.append(cid)
    monkeypatch.setattr(toolbox_admin, "_do_install", fake_install)
    t1 = mc.CONFIRM_MGR_VAR.set(FakeMgr(False))
    t2 = mc.EVENT_QUEUE_VAR.set(asyncio.Queue())
    t3 = mc.SESSION_ID_VAR.set("s1")
    try:
        out = await toolbox_admin.install_component.on_invoke_tool(MagicMock(), '{"component_id": "gh"}')
    finally:
        mc.CONFIRM_MGR_VAR.reset(t1); mc.EVENT_QUEUE_VAR.reset(t2); mc.SESSION_ID_VAR.reset(t3)
    assert called == [] and "denied" in out.lower()
