from mcp_client import status as st


def mk(**kw):
    base = dict(name="测试1", handle="github", status=st.OK, detail="",
                tool_names=["create_issue", "list_prs"], summary="Tools for GitHub.",
                instructions="Full instructions here.", stale=False)
    base.update(kw)
    return st.ServerStatus(**base)


def test_l0_uses_handle_not_user_name():
    """L0's reader is the model, and the model never sees the settings page.
    A user may have typed a name like "测试1" (Chinese for "test 1")."""
    line = st.render_prompt_line(st.McpStatusSnapshot(servers=[mk()]))
    assert "github" in line
    assert "测试1" not in line


def test_l1_lists_every_tool_name_but_no_descriptions():
    lines = st.render_expand_section(st.McpStatusSnapshot(servers=[mk()]))
    text = "\n".join(lines)
    assert "create_issue" in text and "list_prs" in text
    assert "expand as: mcp:github" in text
    assert "Tools for GitHub." in text
    assert "Full instructions here." not in text, "full instructions belong to L2, not L1"


def test_l2_preamble_carries_full_instructions():
    """Full instructions go into the L2 return text, not concatenated into
    every tool's description -- the latter would repeat a server-level
    paragraph 87 times, once per tool."""
    assert "Full instructions here." in st.render_l2_preamble(mk())


def test_degraded_server_is_shown_but_marked_stale():
    """Broken does not mean gone: the model knowing "this capability exists
    but is currently broken" is far more useful than knowing nothing."""
    s = mk(status=st.FAILED, detail="auth failed", stale=True)
    line = st.render_prompt_line(st.McpStatusSnapshot(servers=[s]))
    assert "github" in line and "auth failed" in line
    lines = "\n".join(st.render_expand_section(st.McpStatusSnapshot(servers=[s])))
    assert "create_issue" in lines, "stale tool names still help routing"
    assert "stale" in lines.lower()
