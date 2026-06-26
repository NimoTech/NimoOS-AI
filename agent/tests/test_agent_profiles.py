import agent as agent_module
from profiles import PROFILES
from skills import ALL_TOOLS


def test_select_tools_photos_profile_pinned():
    tools = agent_module.select_tools_for_run(
        [], session_id="s1", profile=PROFILES["photos"])
    names = sorted(t.name for t in tools)
    assert names == ["add_to_album", "create_album", "get_album_summary", "list_albums", "look_at_photos", "rename_album", "search_photos"]


def test_select_tools_photos_ignores_attachments():
    # With a restricted profile the dynamic read_attachment append must NOT
    # happen, even when attachment ids are present. The early return also
    # means no DB access — so no fixture is needed here.
    tools = agent_module.select_tools_for_run(
        ["att-1"], session_id="s1", profile=PROFILES["photos"])
    names = sorted(t.name for t in tools)
    assert names == ["add_to_album", "create_album", "get_album_summary", "list_albums", "look_at_photos", "rename_album", "search_photos"]


def test_select_tools_general_unchanged(monkeypatch):
    monkeypatch.setattr(agent_module, "_fetch_attachments",
                        lambda ids, sid: [])
    tools = agent_module.select_tools_for_run(
        [], session_id="s1", profile=PROFILES["general"])
    names = {t.name for t in tools}
    expected = {t.name for t in ALL_TOOLS} | {"expand_tools"}
    assert names == expected


def test_select_tools_no_profile_kwarg_backward_compat(monkeypatch):
    # Existing call sites without profile= must behave exactly as before.
    monkeypatch.setattr(agent_module, "_fetch_attachments",
                        lambda ids, sid: [])
    tools = agent_module.select_tools_for_run([], session_id="s1")
    names = {t.name for t in tools}
    expected = {t.name for t in ALL_TOOLS} | {"expand_tools"}
    assert names == expected
