"""Run-start rehydration of MCP tools whose gate is already open.

The defect these pin (observed 2026-08-17, session 5554ada9): the unlock gate
is session-scoped — `sessions.unlocked_tool_categories` held `["mcp", "mcp#5"]`
and agent.py reloads it into UNLOCKED_VAR at every run start — but the tools it
authorizes were only ever run-scoped, spliced onto the live Agent object by
mid-run expand_tools. Phoenix spans for that session, in order:

    msgs=4   tools=9    mcp__=0    (nothing expanded yet)
    msgs=6   tools=10   mcp__=0    (after expand_tools(["mcp"]))
    msgs=8   tools=54   mcp__=44   (after expand_tools(["mcp:github"]))
    msgs=10  tools=10   mcp__=0    (next user message -> new run)

The last line is the bug: `add_mcp_server` is still there (static tools are
rebuilt every run and re-gated from the DB) while the 44 `mcp__github__*` are
gone, so the model's `mcp__github__get_me` call raised ModelBehaviorError
"Tool mcp__github__get_me not found in agent NimoOS Agent".
"""
import pytest

from skills import mcp_gating as mg
from skills import tool_gating as tg
import mcp_client.client as mc


def _meta(name):
    return {"name": name, "description": f"desc {name}", "input_schema": {"type": "object"}}


@pytest.fixture
def run_ctx(monkeypatch):
    """Bind the per-run ContextVars agent.py sets before rehydration runs."""
    mg.MCP_HANDLES_VAR.set({"github": 5, "supabase": 8})
    mc._RUN_SERVERS_VAR.set({5: {"id": 5, "name": "Mygithub"},
                             8: {"id": 8, "name": "supabase"}})
    calls = []

    async def fake_metas(server):
        calls.append(server["id"])
        if server["id"] == 5:
            return [_meta("get_me"), _meta("list_issues")], mc.OK, ""
        return [_meta("search_docs")], mc.OK, ""

    monkeypatch.setattr(mc, "_metas_for_server", fake_metas)
    return calls


@pytest.mark.asyncio
async def test_open_gate_is_rebuilt_at_run_start(run_ctx):
    tg.UNLOCKED_VAR.set({"mcp", "mcp#5"})
    tools, loaded = await tg.rehydrate_unlocked_mcp_tools()
    assert [t.name for t in tools] == ["mcp__github__get_me", "mcp__github__list_issues"]
    assert loaded == {"github"}


@pytest.mark.asyncio
async def test_closed_gate_costs_nothing(run_ctx):
    """A fresh session's first turn must do literally zero work here — that is
    what keeps first-token latency untouched (Task 16's contract)."""
    tg.UNLOCKED_VAR.set({"mcp"})
    tools, loaded = await tg.rehydrate_unlocked_mcp_tools()
    assert tools == [] and loaded == set()
    assert run_ctx == [], "a closed gate must not even ask for schemas"


@pytest.mark.asyncio
async def test_only_the_opened_server_is_rebuilt(run_ctx):
    tg.UNLOCKED_VAR.set({"mcp#8"})
    tools, loaded = await tg.rehydrate_unlocked_mcp_tools()
    assert [t.name for t in tools] == ["mcp__supabase__search_docs"]
    assert loaded == {"supabase"} and run_ctx == [8]


@pytest.mark.asyncio
async def test_gate_for_a_server_no_longer_in_this_run_is_skipped(run_ctx):
    """Deleted or disabled in settings: the key stays in the persisted set (it
    must — disabling and re-enabling must not revoke anything) but there is no
    server to load, and that must not raise."""
    tg.UNLOCKED_VAR.set({"mcp#5", "mcp#999"})
    tools, loaded = await tg.rehydrate_unlocked_mcp_tools()
    assert loaded == {"github"} and run_ctx == [5]


@pytest.mark.asyncio
async def test_config_error_server_is_never_rebuilt(monkeypatch):
    """Go flagged its stored credentials as undecryptable; _build_mcp_for_run
    refuses to advertise it as connectable, so its tools must not be loaded
    either."""
    mg.MCP_HANDLES_VAR.set({"broken": 9})
    mc._RUN_SERVERS_VAR.set({9: {"id": 9, "name": "broken", "config_error": "decrypt failed"}})
    called = []

    async def fake_metas(server):
        called.append(server["id"])
        return [_meta("x")], mc.OK, ""

    monkeypatch.setattr(mc, "_metas_for_server", fake_metas)
    tg.UNLOCKED_VAR.set({"mcp#9"})
    tools, loaded = await tg.rehydrate_unlocked_mcp_tools()
    assert tools == [] and loaded == set() and called == []


@pytest.mark.asyncio
async def test_degraded_fetch_injects_nothing_and_reports_nothing_loaded(monkeypatch):
    """fetch_schemas degrades to (0, []) on any network/HTTP/parse failure, and
    _metas_for_server turns that into ([], FAILED, reason). Injecting a
    half-trusted list, or claiming the server is loaded, is worse than loading
    nothing: the L0 line would then tell the model to call tools that aren't
    there."""
    mg.MCP_HANDLES_VAR.set({"github": 5})
    mc._RUN_SERVERS_VAR.set({5: {"id": 5, "name": "Mygithub"}})

    async def fake_metas(server):
        return [], mc.FAILED, "could not fetch tool schemas from nimoos-ai"

    monkeypatch.setattr(mc, "_metas_for_server", fake_metas)
    tg.UNLOCKED_VAR.set({"mcp#5"})
    tools, loaded = await tg.rehydrate_unlocked_mcp_tools()
    assert tools == [] and loaded == set()


@pytest.mark.asyncio
async def test_never_raises_into_the_run(monkeypatch):
    """MCP is additive: a failure here must not stop the run from starting."""
    mg.MCP_HANDLES_VAR.set({"github": 5})
    mc._RUN_SERVERS_VAR.set({5: {"id": 5, "name": "Mygithub"}})

    async def boom(server):
        raise RuntimeError("loopback down")

    monkeypatch.setattr(mc, "_metas_for_server", boom)
    tg.UNLOCKED_VAR.set({"mcp#5"})
    assert await tg.rehydrate_unlocked_mcp_tools() == ([], set())


@pytest.mark.asyncio
async def test_rehydrated_tools_are_not_is_enabled_gated(run_ctx):
    """Selection happens at BUILD time (only open gates are built), never via
    an is_enabled callback. estimate_tools_tokens walks the whole list without
    consulting is_enabled, so a gated-but-present MCP tool would inflate the
    compaction overhead by its full schema cost while contributing nothing."""
    tg.UNLOCKED_VAR.set({"mcp#5"})
    tools, _ = await tg.rehydrate_unlocked_mcp_tools()
    assert tools and all(t.is_enabled is True for t in tools)


# --- the recovery path when a tool genuinely isn't loaded --------------------

def test_tool_not_found_message_names_the_gate_to_open():
    mg.MCP_HANDLES_VAR.set({"github": 5})
    msg = mg.tool_not_found_message("mcp__github__get_me")
    assert 'expand_tools(["mcp:github"])' in msg
    assert "mcp__github__get_me" in msg


def test_tool_not_found_message_prefers_the_longest_matching_slug():
    """Slugs and tool names both contain underscores, so splitting on "__"
    cannot recover the boundary: "mcp__github_2__get_me" must resolve to the
    server actually named github_2, not to github with a tool named "2__get_me".
    """
    mg.MCP_HANDLES_VAR.set({"github": 5, "github_2": 9})
    msg = mg.tool_not_found_message("mcp__github_2__get_me")
    assert 'expand_tools(["mcp:github_2"])' in msg


def test_tool_not_found_message_for_unknown_server_sends_model_to_catalogue():
    mg.MCP_HANDLES_VAR.set({"github": 5})
    msg = mg.tool_not_found_message("mcp__notion__search")
    assert 'expand_tools(["mcp"])' in msg
    assert "mcp:notion" not in msg, "never point at a gate that cannot be opened"


def test_tool_not_found_message_ignores_non_mcp_tools():
    """The SDK's own "Tool 'x' not found." is the right message for those."""
    mg.MCP_HANDLES_VAR.set({"github": 5})
    assert mg.tool_not_found_message("read_file") is None
