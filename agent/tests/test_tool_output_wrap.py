import dataclasses

import pytest
from agents import function_tool

import tool_output as to


@function_tool
async def big_tool(n: int) -> str:
    """Return n x's."""
    return "x" * n


class Ctx:  # stand-in for agents.tool_context.ToolContext
    def __init__(self, call_id="call_1"):
        self.tool_call_id = call_id
        # installed agents==0.17.2's real on_invoke_tool reads these two
        # attributes (tool_name while parsing input, run_config on any
        # error path) even for a tool that doesn't declare `takes_context`.
        self.tool_name = "big_tool"
        self.run_config = None


@pytest.mark.asyncio
async def test_wrapped_tool_offloads_and_sets_call_id(tmp_path):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    seen = {}
    orig = big_tool.on_invoke_tool

    async def spy(ctx, args):
        seen["call_id"] = to.CALL_ID_VAR.get("")
        return await orig(ctx, args)

    tool = dataclasses.replace(big_tool, on_invoke_tool=spy)
    w = to.wrap_tool_output(tool)
    out = await w.on_invoke_tool(Ctx("call_9"), '{"n": %d}' % (to.OFFLOAD_THRESHOLD_CHARS + 5))
    assert to.TRAILER_RE.search(out)
    assert (tmp_path / "call_9.txt").exists()
    assert seen["call_id"] == "call_9"
    assert to.CALL_ID_VAR.get("") == ""          # reset after the call


@pytest.mark.asyncio
async def test_wrapped_tool_small_output_unchanged(tmp_path):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    w = to.wrap_tool_output(big_tool)
    assert await w.on_invoke_tool(Ctx(), '{"n": 10}') == "x" * 10


def test_wrap_is_idempotent_and_keeps_identity():
    w1 = to.wrap_tool_output(big_tool)
    w2 = to.wrap_tool_output(w1)
    assert w2 is w1
    assert w1.name == big_tool.name
    assert to.is_wrapped(w1) and not to.is_wrapped(big_tool)


def test_wrap_passes_non_function_tools_through():
    sentinel = object()
    assert to.wrap_tool_output(sentinel) is sentinel


@pytest.mark.asyncio
async def test_wrapper_never_raises_from_postprocess(tmp_path, monkeypatch):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(to, "postprocess", boom)
    w = to.wrap_tool_output(big_tool)
    out = await w.on_invoke_tool(Ctx(), '{"n": %d}' % (to.OFFLOAD_THRESHOLD_CHARS + 5))
    assert out == "x" * (to.OFFLOAD_THRESHOLD_CHARS + 5)
