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
    assert "install_app" in out          # 返回了该类工具清单
    assert written["cats"] == ["apps"] or "apps" in written["cats"]


def test_expand_unknown_category_returns_error(monkeypatch):
    monkeypatch.setattr(tg, "_persist", lambda cats: None)
    tg.UNLOCKED_VAR.set(set())
    tg.GATING_SESSION_VAR.set("s1")
    out = tg.expand_categories(["bogus"])
    assert "bogus" in out
    assert ("apps" in out and "files" in out)   # 错误信息列出合法类别
    assert "bogus" not in tg.current_unlocked()


def test_expand_empty_returns_overview(monkeypatch):
    monkeypatch.setattr(tg, "_persist", lambda cats: None)
    tg.UNLOCKED_VAR.set(set())
    tg.GATING_SESSION_VAR.set("s1")
    out = tg.expand_categories([])
    assert "apps" in out and "photos" in out
