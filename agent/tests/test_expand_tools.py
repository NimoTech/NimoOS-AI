from skills import tool_gating as tg


def test_overview_lists_all_categories():
    txt = tg.categories_overview()
    for cat in ("apps", "files", "photos", "wiki",
                "documents", "system", "events", "mcp"):
        assert cat in txt


def test_expand_unlocks_and_lists(monkeypatch):
    written = {}
    monkeypatch.setattr(tg, "_persist",
                        lambda cats: written.setdefault("cats", cats))
    tg.UNLOCKED_VAR.set(set())
    tg.GATING_SESSION_VAR.set("s1")
    out = tg.expand_categories(["apps"])
    assert "apps" in tg.current_unlocked()
    assert "install_app" in out          # returns that category's tool list
    assert written["cats"] == ["apps"] or "apps" in written["cats"]


def test_expand_unknown_category_returns_error(monkeypatch):
    monkeypatch.setattr(tg, "_persist", lambda cats: None)
    tg.UNLOCKED_VAR.set(set())
    tg.GATING_SESSION_VAR.set("s1")
    out = tg.expand_categories(["bogus"])
    assert "bogus" in out
    assert ("apps" in out and "files" in out)   # error message lists valid categories
    assert "bogus" not in tg.current_unlocked()


def test_expand_empty_returns_overview(monkeypatch):
    monkeypatch.setattr(tg, "_persist", lambda cats: None)
    tg.UNLOCKED_VAR.set(set())
    tg.GATING_SESSION_VAR.set("s1")
    out = tg.expand_categories([])
    assert "apps" in out and "photos" in out


from mcp_client import status as st


def _prep(monkeypatch, snap):
    monkeypatch.setattr(tg, "_persist", lambda cats: None)
    tg.UNLOCKED_VAR.set(set())
    tg.GATING_SESSION_VAR.set("s1")
    st.MCP_STATUS_VAR.set(snap)


def test_expand_mcp_lists_runtime_tools_and_status(monkeypatch):
    # Defect 2: the authoritative unlock feedback used to list ONLY the static
    # mcp_register_server, telling the model no servers were connected.
    _prep(monkeypatch, st.McpStatusSnapshot(servers=[
        st.ServerStatus(name="Mygithub", status=st.OK,
                        tool_names=["mcp__mygithub__search", "mcp__mygithub__get_issue"]),
        st.ServerStatus(name="supabase", status=st.FAILED, detail="timeout"),
    ]))
    out = tg.expand_categories(["mcp"])
    # The admin tool must read as a "system tool" line, visually distinct from
    # (and same level as) the per-server tool lists, so the model doesn't treat
    # it as the only callable tool with the servers as its sub-items.
    assert "System tool: add_mcp_server;" in out
    assert "- add_mcp_server" not in out   # no bullet — same level as server lines
    assert "mcp__mygithub__search" in out          # real runtime tools now listed
    assert "supabase" in out and "timeout" in out  # failure re-disclosed at unlock time
    assert "Do not register replacement" in out


def test_expand_already_unlocked_says_tools_are_in_list(monkeypatch):
    # re-unlocking must tell the model the tools above are ALREADY in its tool
    # list (schemas included), not just that nothing changed
    _prep(monkeypatch, st.McpStatusSnapshot(servers=[
        st.ServerStatus(name="Mygithub", status=st.OK,
                        tool_names=["mcp__mygithub__get_me"])]))
    tg.expand_categories(["mcp"])
    out = tg.expand_categories(["mcp"])
    assert "already unlocked" in out
    assert "ALREADY in your tool list" in out
    assert "call it directly" in out


def test_expand_mcp_no_servers_is_explicit(monkeypatch):
    _prep(monkeypatch, st.McpStatusSnapshot())
    out = tg.expand_categories(["mcp"])
    assert "No MCP servers are configured." in out


def test_expand_mcp_missing_snapshot_falls_back(monkeypatch):
    # 2B fallback: never lie, never fall back to the static table alone.
    _prep(monkeypatch, None)
    out = tg.expand_categories(["mcp"])
    assert "appear in your tool list on the next step" in out
    assert "add_mcp_server" in out


def test_expand_non_mcp_categories_untouched(monkeypatch):
    _prep(monkeypatch, st.McpStatusSnapshot(servers=[
        st.ServerStatus(name="g", status=st.OK, tool_names=["mcp__g__t"])]))
    out = tg.expand_categories(["apps"])
    assert "mcp__g__t" not in out and "MCP server" not in out
