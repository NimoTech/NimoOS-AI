from mcp_client import status as st


def _snap(*servers, config_error=""):
    return st.McpStatusSnapshot(servers=list(servers), config_error=config_error)


def test_status_constants():
    assert (st.OK, st.FAILED, st.WARMING, st.CONFIG_ERROR) == (
        "ok", "failed", "warming", "config_error")


# --- render_prompt_line ---

def test_prompt_line_none_snapshot_is_empty():
    assert st.render_prompt_line(None) == ""


def test_prompt_line_no_servers_is_empty():
    # no servers configured: inject nothing (zero cost)
    assert st.render_prompt_line(_snap()) == ""


def test_prompt_line_mixed_statuses():
    line = st.render_prompt_line(_snap(
        st.ServerStatus(name="Mygithub", status=st.OK, tool_names=["a"] * 32),
        st.ServerStatus(name="supabase", status=st.FAILED, detail="timeout"),
        st.ServerStatus(name="fs", status=st.WARMING),
        st.ServerStatus(name="old", status=st.CONFIG_ERROR, detail="decrypt failed"),
    ))
    assert line.startswith("[MCP servers: ") and line.endswith("]")
    # No slug and no fq tool names to recover one from -> no expand token to
    # advertise, but still never a bare "ready" (see the two tests below).
    assert "Mygithub: 32 tools, not loaded yet" in line
    assert "supabase: failed to load (timeout)" in line
    assert "fs: still connecting, not loadable yet" in line
    assert "old: configuration error (decrypt failed)" in line


def test_prompt_line_unloaded_server_names_the_gate_instead_of_claiming_ready():
    # Regression, the whole point of this branch's fix: a server whose gate is
    # NOT open this run has no tools in the request's tool array. The old
    # wording ("N tools ready") read as callable, so the model called
    # mcp__<slug>__<tool> directly and the SDK raised "Tool ... not found in
    # agent", killing the turn. The line must instead name the exact token
    # that loads them.
    line = st.render_prompt_line(_snap(
        st.ServerStatus(name="Mygithub", status=st.OK, slug="github",
                        tool_names=["mcp__github__get_me"] * 44)))
    assert 'github: 44 tools, load with expand_tools(["mcp:github"])' in line
    assert "ready" not in line


def test_prompt_line_loaded_server_says_the_tools_are_callable_now():
    # After run-start rehydration (skills.tool_gating.rehydrate_unlocked_mcp_tools)
    # the tools ARE in the tool array, so the line must not send the model
    # through expand_tools for them again.
    line = st.render_prompt_line(_snap(
        st.ServerStatus(name="Mygithub", status=st.OK, slug="github", loaded=True,
                        tool_names=["mcp__github__get_me"] * 44)))
    assert "github: 44 tools, already in your tool list" in line
    assert "expand_tools" not in line


def test_expand_section_loaded_server_does_not_advertise_its_gate():
    lines = st.render_expand_section(_snap(
        st.ServerStatus(name="Mygithub", status=st.OK, slug="github", loaded=True,
                        tool_names=["mcp__github__get_me"])))
    joined = "\n".join(lines)
    assert "already in your tool list — call them directly;" in joined
    assert "expand as:" not in joined


def test_prompt_line_ok_server_with_zero_tools_is_not_misrepresented_as_ready():
    # A server that probes ok but publishes zero tools (e.g. a real remote
    # server that requires auth this repo does not try to detect) must not
    # read as "0 tools ready" -- that phrasing implies a working, expandable
    # server, which this one is not.
    line = st.render_prompt_line(_snap(
        st.ServerStatus(name="needsauth", status=st.OK, tool_names=[])))
    assert "0 tools ready" not in line
    assert "needsauth: connected, published no tools" in line


def test_prompt_line_config_unavailable():
    line = st.render_prompt_line(_snap(config_error="HTTP 500"))
    assert "configuration could not be fetched" in line
    assert "HTTP 500" in line
    assert "do not register new" in line.lower()


def test_prompt_line_detail_truncated():
    line = st.render_prompt_line(_snap(
        st.ServerStatus(name="x", status=st.FAILED, detail="e" * 500)))
    assert len(line) < 220          # status line's token cost must stay bounded (~20-40 tokens)


# --- render_expand_section ---

def test_expand_none_snapshot_falls_back():
    # 2B fallback: never lie when snapshot is missing, never fall back to static table
    lines = st.render_expand_section(None)
    assert lines == ["MCP runtime tools, if any, appear in your tool list on the next step."]


def test_expand_no_servers():
    assert st.render_expand_section(_snap()) == ["No MCP servers are configured."]


def test_expand_ok_server_lists_tools():
    lines = st.render_expand_section(_snap(
        st.ServerStatus(name="Mygithub", status=st.OK,
                        tool_names=["mcp__mygithub__search", "mcp__mygithub__get_issue"])))
    joined = "\n".join(lines)
    assert '"Mygithub" (2 tools)' in joined
    assert "mcp__mygithub__search" in joined and "mcp__mygithub__get_issue" in joined
    # each server's tool list ends with ";" so the model can tell where one
    # server's list stops and the next line begins
    assert lines[0].endswith(";")


def test_expand_long_tool_list_not_truncated():
    # every tool must be listed by name — an elided "… (40 total)" hid names
    # from the model, so it couldn't call them and drifted to other tools
    names = [f"mcp__s__tool{i}" for i in range(40)]
    joined = "\n".join(st.render_expand_section(_snap(
        st.ServerStatus(name="s", status=st.OK, tool_names=names))))
    for n in names:
        assert n in joined
    assert "…" not in joined
    assert "(40 tools)" in joined and joined.rstrip().endswith(";")


def test_expand_ok_server_with_zero_tools_has_no_dangling_punctuation_or_expand_hint():
    # Before this fix, an OK status with tool_names == [] fell into the
    # generic OK branch and produced a malformed 'MCP server "x" (0 tools): ;'
    # line PLUS an "expand as:" hint -- inviting the model to open a gate
    # with nothing behind it. Opening it used to report "no tool schemas
    # could be loaded right now -- try again shortly", which is untrue: there
    # is nothing to load, and retrying will not help. Both real enabled
    # servers on the live machine are in exactly this state.
    joined = "\n".join(st.render_expand_section(_snap(
        st.ServerStatus(name="needsauth", status=st.OK, tool_names=[],
                        summary="a remote server"))))
    assert ": ;" not in joined
    assert "expand as:" not in joined
    assert '"needsauth": connected, but published no tools' in joined
    assert "a remote server" in joined


def test_expand_failed_server_discloses_and_warns():
    joined = "\n".join(st.render_expand_section(_snap(
        st.ServerStatus(name="supabase", status=st.FAILED, detail="timeout"))))
    assert '"supabase"' in joined and "timeout" in joined
    assert "Do not register replacement" in joined   # guards against duplicate-registration behavior in field


def test_expand_warming_server_forbids_retrying_in_this_turn():
    """Regression for run debe6e65: the old text ("its tools should appear on a
    later message") read as "a later step in this turn", so the model called
    expand_tools(["mcp"]) three times against an identical answer and burned the
    whole turn. Go's probe is a background goroutine kicked off by the Runtime
    GET at run start and cannot finish mid-turn, so the model must be told to
    stop, not to wait."""
    joined = "\n".join(st.render_expand_section(_snap(
        st.ServerStatus(name="fs", status=st.WARMING))))
    assert '"fs"' in joined
    assert "cannot be loaded yet" in joined
    assert "THIS turn cannot change that" in joined
    assert "ask again in a moment" in joined
    assert "later message" not in joined, "must not read as 'a later step in this turn'"


def test_expand_config_unavailable():
    joined = "\n".join(st.render_expand_section(_snap(config_error="no ticket")))
    assert "could not be fetched" in joined and "no ticket" in joined
    assert "registering" in joined      # also discourages registering new servers


def test_l1_names_are_marked_not_callable_until_the_server_is_expanded():
    """L1 publishes tool NAMES only — no FunctionTool reaches the tool array
    until expand_tools(["mcp:<slug>"]) builds them (tool_gating._fetch_and_build).
    The old hint ("expand as: mcp:github for full tool schemas") read as an
    optional detail upgrade, so the model treated the names printed right above
    it as callable and called one, which is the same "not found in agent" wall
    the cross-run gap produced. The precondition has to be stated, not hinted."""
    lines = st.render_expand_section(_snap(
        st.ServerStatus(name="Mygithub", status=st.OK, slug="github",
                        tool_names=["mcp__github__get_me", "mcp__github__list_issues"])))
    joined = "\n".join(lines)
    assert "mcp__github__get_me" in joined                    # names still published
    assert 'NOT callable yet — expand_tools(["mcp:github"]) loads them' in joined
    assert "for full tool schemas" not in joined
