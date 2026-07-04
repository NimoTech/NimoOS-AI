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


def _fake_conn_with_source(source):
    class _Cur:
        def fetchone(self):
            return {"source": source} if source is not None else None

    class _Conn:
        def execute(self, *a, **k):
            return _Cur()
    return _Conn()


def test_select_tools_channel_source_appends_send_attachment(monkeypatch):
    # channel-sourced session (source != 'web') gets the outbound file tool.
    monkeypatch.setattr(agent_module, "_fetch_attachments",
                        lambda ids, sid: [])
    import db as db_module
    monkeypatch.setattr(db_module, "get_connection",
                        lambda: _fake_conn_with_source("telegram"))
    tools = agent_module.select_tools_for_run(
        [], session_id="chan1", profile=PROFILES["general"])
    names = {t.name for t in tools}
    assert "send_attachment" in names


def test_select_tools_web_source_omits_send_attachment(monkeypatch):
    # web-sourced session (source == 'web') never gets the outbound file tool.
    monkeypatch.setattr(agent_module, "_fetch_attachments",
                        lambda ids, sid: [])
    import db as db_module
    monkeypatch.setattr(db_module, "get_connection",
                        lambda: _fake_conn_with_source("web"))
    tools = agent_module.select_tools_for_run(
        [], session_id="web1", profile=PROFILES["general"])
    names = {t.name for t in tools}
    assert "send_attachment" not in names
