import agent as agent_mod
from skills import tool_gating as tg
from skills import tool_registry as tr


def _names(tools):
    return {getattr(t, "name", getattr(t, "__name__", "")) for t in tools}


def test_general_turn1_visible_is_core_plus_expand():
    tools = agent_mod.select_tools_for_run([], session_id="s1", profile=None)
    names = _names(tools)
    # 6 always-on tools + expand_tools must always be present
    assert tr.CORE_TOOL_NAMES <= names
    assert "expand_tools" in names
    # gated tool objects exist but default to not visible (is_enabled is False with an empty unlocked set)
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
    # gating must not mutate the shared original's is_enabled (original stays default True)
    from skills.app_management import ALL_TOOLS as APP_TOOLS
    orig = APP_TOOLS[0]
    agent_mod.select_tools_for_run([], session_id="s1", profile=None)
    assert orig.is_enabled is True
    assert not callable(orig.is_enabled)   # original's is_enabled is still boolean True, not swapped for a gating callback


class _PinnedProfile:
    tools = ("a", "b")          # placeholder: pinned returns a fixed set


def test_pinned_profile_unchanged():
    tools = agent_mod.select_tools_for_run([], session_id="s1", profile=_PinnedProfile())
    assert list(tools) == ["a", "b"]
    assert "expand_tools" not in _names(tools)
