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
    # 没有配置任何服务器时不注入（零成本）
    assert st.render_prompt_line(_snap()) == ""


def test_prompt_line_mixed_statuses():
    line = st.render_prompt_line(_snap(
        st.ServerStatus(name="Mygithub", status=st.OK, tool_names=["a"] * 32),
        st.ServerStatus(name="supabase", status=st.FAILED, detail="timeout"),
        st.ServerStatus(name="fs", status=st.WARMING),
        st.ServerStatus(name="old", status=st.CONFIG_ERROR, detail="decrypt failed"),
    ))
    assert line.startswith("[MCP servers: ") and line.endswith("]")
    assert "Mygithub: 32 tools ready" in line
    assert "supabase: failed to load (timeout)" in line
    assert "fs: starting up" in line
    assert "old: configuration error (decrypt failed)" in line


def test_prompt_line_config_unavailable():
    line = st.render_prompt_line(_snap(config_error="HTTP 500"))
    assert "configuration could not be fetched" in line
    assert "HTTP 500" in line
    assert "do not register new" in line.lower()


def test_prompt_line_detail_truncated():
    line = st.render_prompt_line(_snap(
        st.ServerStatus(name="x", status=st.FAILED, detail="e" * 500)))
    assert len(line) < 220          # 状态行成本必须有界(约 20-40 token)


# --- render_expand_section ---

def test_expand_none_snapshot_falls_back():
    # 2B 兜底:快照缺失时永不撒谎,也绝不回落到静态表
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


def test_expand_long_tool_list_truncated():
    names = [f"mcp__s__tool{i}" for i in range(40)]
    joined = "\n".join(st.render_expand_section(_snap(
        st.ServerStatus(name="s", status=st.OK, tool_names=names))))
    assert "mcp__s__tool0" in joined and "mcp__s__tool14" in joined
    assert "mcp__s__tool15" not in joined      # 截断列前 15 个
    assert "(40 total)" in joined              # + 总数


def test_expand_failed_server_discloses_and_warns():
    joined = "\n".join(st.render_expand_section(_snap(
        st.ServerStatus(name="supabase", status=st.FAILED, detail="timeout"))))
    assert '"supabase"' in joined and "timeout" in joined
    assert "Do not register replacement" in joined   # 防实录里的重复注册行为


def test_expand_warming_server():
    joined = "\n".join(st.render_expand_section(_snap(
        st.ServerStatus(name="fs", status=st.WARMING))))
    assert '"fs"' in joined and "later message" in joined


def test_expand_config_unavailable():
    joined = "\n".join(st.render_expand_section(_snap(config_error="no ticket")))
    assert "could not be fetched" in joined and "no ticket" in joined
    assert "registering" in joined      # 同样劝阻注册新服务器
