import asyncio
import inspect
import json

import pytest

import agent as agent_module
import tool_output as to


def test_select_tools_for_run_wraps_every_tool(monkeypatch):
    monkeypatch.setattr(agent_module, "_fetch_attachments", lambda ids, sid: [])
    monkeypatch.setattr(agent_module, "_web_search_available", lambda: True)
    tools = agent_module.select_tools_for_run([], session_id="s-wrap")
    names = [getattr(t, "name", "") for t in tools]
    assert "read_file" in names and "expand_tools" in names and "web_fetch" in names
    unwrapped = [n for t, n in zip(tools, names) if not to.is_wrapped(t)]
    assert unwrapped == []


def test_select_tools_for_run_keeps_gating_is_enabled(monkeypatch):
    monkeypatch.setattr(agent_module, "_fetch_attachments", lambda ids, sid: [])
    tools = agent_module.select_tools_for_run([], session_id="s-wrap2")
    write_file = next(t for t in tools if t.name == "write_file")
    assert callable(write_file.is_enabled)          # gated copy still gated
    read_file = next(t for t in tools if t.name == "read_file")
    assert read_file.is_enabled is True             # core stays always-on


def test_run_sets_offload_vars_source_contains_wiring():
    # Pin the wiring by source (the run() method needs a live provider to execute).
    src = inspect.getsource(agent_module.AgentRunner.run)
    assert "OFFLOAD_DIR_VAR.set(" in src and "ensure_offload_dir(" in src
    assert "RUN_SCRATCH_VAR.set({})" in src


@pytest.mark.asyncio
async def test_mcp_wrap_tool_postprocesses_result(monkeypatch, tmp_path):
    from mcp_client import client as mc
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    big = "z" * (to.OFFLOAD_THRESHOLD_CHARS + 10)

    class FakeConn:
        async def call_tool(self, name, args):
            return big

    async def fake_get_run_conn(server):
        return FakeConn()

    async def fake_confirmed(server, tool_name, args):
        return True

    monkeypatch.setattr(mc, "_get_run_conn", fake_get_run_conn)
    monkeypatch.setattr(mc, "_ensure_confirmed", fake_confirmed)
    monkeypatch.setattr(mc, "flatten_result", lambda r: r)
    mc.USER_PATTERNS_VAR.set([])
    tool = mc._wrap_tool({"id": 1, "name": "srv"}, {"name": "dump", "input_schema": {}}, "srv")

    class Ctx:
        tool_call_id = "call_mcp1"

    out = await tool.on_invoke_tool(Ctx(), json.dumps({}))
    assert to.TRAILER_RE.search(out)
    assert (tmp_path / "call_mcp1.txt").read_text(encoding="utf-8") == big
