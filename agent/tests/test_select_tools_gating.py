import agent as agent_mod
from skills import tool_gating as tg
from skills import tool_registry as tr


def _names(tools):
    return {getattr(t, "name", getattr(t, "__name__", "")) for t in tools}


def test_general_turn1_visible_is_core_plus_expand():
    tools = agent_mod.select_tools_for_run([], session_id="s1", profile=None)
    names = _names(tools)
    # 常驻 6 + expand_tools 一定在
    assert tr.CORE_TOOL_NAMES <= names
    assert "expand_tools" in names
    # 门控工具对象存在但默认不可见(is_enabled 在空解锁集下为 False)
    tg.UNLOCKED_VAR.set(set())
    by_name = {getattr(t, "name", ""): t for t in tools}
    install = by_name["install_app"]
    assert install.is_enabled(None, None) is False


def test_general_unlocked_category_visible():
    tools = agent_mod.select_tools_for_run([], session_id="s1", profile=None)
    by_name = {getattr(t, "name", ""): t for t in tools}
    tg.UNLOCKED_VAR.set({"apps"})
    assert by_name["install_app"].is_enabled(None, None) is True


def test_core_objects_not_mutated():
    # 门控不能改共享原件的 is_enabled(原件仍为默认 True)
    from skills.app_management import ALL_TOOLS as APP_TOOLS
    orig = APP_TOOLS[0]
    agent_mod.select_tools_for_run([], session_id="s1", profile=None)
    assert orig.is_enabled is True


class _PinnedProfile:
    tools = ("a", "b")          # 占位:pinned 返回固定集


def test_pinned_profile_unchanged():
    tools = agent_mod.select_tools_for_run([], session_id="s1", profile=_PinnedProfile())
    assert list(tools) == ["a", "b"]
    assert "expand_tools" not in _names(tools)
