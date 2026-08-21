"""permissions.py — global permission policy: load/save, gate mapping,
context resolution, shell caps, judge toggles."""
from __future__ import annotations

import json

import db as db_module
import permissions


def _conn():
    return db_module.init_db(":memory:")


# -- load/save --------------------------------------------------------------

def test_load_defaults_when_absent():
    policy = permissions.load(_conn())
    assert policy == permissions.default_policy()
    assert policy["gates"]["shell"] == "ask"
    assert policy["contexts"] == {"tasks": "strict", "channels": "strict"}
    assert policy["judges"] == {"shell": True, "egress": True}


def test_save_then_load_roundtrip():
    conn = _conn()
    saved = permissions.save(conn, {
        "preset": "balanced",
        "gates": {"notes": "auto", "shell": "auto_gray"},
        "judges": {"egress": False},
        "contexts": {"tasks": "follow"},
        "proxy": {"tofu_ttl_hours": 24},
    })
    loaded = permissions.load(conn)
    assert loaded == saved
    assert loaded["preset"] == "balanced"
    assert loaded["gates"]["notes"] == "auto"
    assert loaded["gates"]["shell"] == "auto_gray"
    assert loaded["gates"]["apps"] == "ask"          # untouched → default
    assert loaded["judges"] == {"shell": True, "egress": False}
    assert loaded["contexts"] == {"tasks": "follow", "channels": "strict"}
    assert loaded["proxy"]["tofu_ttl_hours"] == 24
    assert loaded["proxy"]["upload_threshold_kb"] == 64


def test_save_coerces_invalid_values_back_to_defaults():
    conn = _conn()
    saved = permissions.save(conn, {
        "preset": "yolo",
        "gates": {"shell": "auto", "notes": "always", "bogus": "auto"},
        "judges": {"shell": "no"},
        "contexts": {"tasks": "wild"},
        "proxy": {"tofu_ttl_hours": 0, "upload_threshold_kb": True},
    })
    assert saved == permissions.default_policy()
    assert "bogus" not in saved["gates"]


def test_corrupt_json_degrades_to_defaults():
    conn = _conn()
    conn.execute(
        "INSERT INTO user_settings(user_id, key, value, updated_at) "
        "VALUES('__global__', 'permission_policy', 'not json', 0)")
    conn.commit()
    assert permissions.load(conn) == permissions.default_policy()


def test_proxy_bounds_enforced():
    conn = _conn()
    saved = permissions.save(conn, {"proxy": {
        "tofu_ttl_hours": permissions.TOFU_TTL_HOURS_MAX + 1,
        "upload_threshold_kb": permissions.UPLOAD_THRESHOLD_KB_MAX + 1}})
    assert saved["proxy"] == {"tofu_ttl_hours": 1, "upload_threshold_kb": 64}


# -- gate mapping -----------------------------------------------------------

def test_gate_of_exact_and_prefix():
    assert permissions.gate_of("install_app") == "apps"
    assert permissions.gate_of("uninstall_app") == "apps"
    assert permissions.gate_of("trigger_action") == "message_bus"
    assert permissions.gate_of("notes_update") == "notes"
    assert permissions.gate_of("wiki_register_root") == "wiki"
    assert permissions.gate_of("grant_access") == "fs_access"
    assert permissions.gate_of("shell_command") == "shell"
    assert permissions.gate_of("shell_network") == "network"
    assert permissions.gate_of("egress_upload") == "upload"
    assert permissions.gate_of("egress") == "network"
    assert permissions.gate_of("mcp_install:foo") == "installs"
    assert permissions.gate_of("toolbox_install:gh") == "installs"
    assert permissions.gate_of("mcp_call:srv::tool") == "mcp_tools"


def test_gate_of_never_auto_for_elicitation_or_unknown():
    assert permissions.gate_of("mcp_elicit:srv") is None
    assert permissions.gate_of("something_else") is None
    assert permissions.gate_of(None) is None


# -- auto_approve: interactive ----------------------------------------------

def test_interactive_defaults_ask_everything():
    conn = _conn()
    for action in ("install_app", "notes_write", "grant_access",
                   "mcp_call:s::t", "egress", "egress_upload"):
        assert permissions.auto_approve(conn, action) is False


def test_interactive_auto_gate_opens_only_that_gate():
    conn = _conn()
    permissions.save(conn, {"gates": {"notes": "auto"}})
    assert permissions.auto_approve(conn, "notes_write") is True
    assert permissions.auto_approve(conn, "notes_update") is True
    assert permissions.auto_approve(conn, "wiki_append_notes") is False
    assert permissions.auto_approve(conn, "grant_access") is False


def test_interactive_shell_levels():
    conn = _conn()
    permissions.save(conn, {"gates": {"shell": "auto_gray"}})
    assert permissions.auto_approve(conn, "shell_command", level="gray") is True
    assert permissions.auto_approve(conn, "shell_command", level="dangerous") is False
    assert permissions.auto_approve(conn, "shell_command", level="protected") is False
    permissions.save(conn, {"gates": {"shell": "auto_all"}})
    assert permissions.auto_approve(conn, "shell_command", level="dangerous") is True
    assert permissions.auto_approve(conn, "shell_command", level="protected") is False


def test_elicitation_never_auto_even_when_everything_is_open():
    conn = _conn()
    permissions.save(conn, {
        "gates": {k: ("auto_all" if k == "shell" else "auto")
                  for k in permissions.default_policy()["gates"]},
        "contexts": {"tasks": "auto", "channels": "auto"},
    })
    assert permissions.auto_approve(conn, "mcp_elicit:srv") is False
    assert permissions.auto_approve(conn, "mcp_elicit:srv", context="task") is False


# -- auto_approve: contexts ---------------------------------------------------

def test_task_strict_never_auto_even_if_gate_is_auto():
    conn = _conn()
    permissions.save(conn, {"gates": {"notes": "auto"}})
    assert permissions.auto_approve(conn, "notes_write", context="task") is False


def test_task_follow_uses_gate_policy():
    conn = _conn()
    permissions.save(conn, {"gates": {"notes": "auto"},
                            "contexts": {"tasks": "follow"}})
    assert permissions.auto_approve(conn, "notes_write", context="task") is True
    assert permissions.auto_approve(conn, "wiki_append_notes", context="task") is False


def test_task_auto_approves_everything_but_caps_shell_at_gray():
    conn = _conn()
    permissions.save(conn, {"gates": {"shell": "auto_all"},
                            "contexts": {"tasks": "auto"}})
    assert permissions.auto_approve(conn, "install_app", context="task") is True
    assert permissions.auto_approve(conn, "egress", context="task") is True
    assert permissions.auto_approve(conn, "shell_command", level="gray",
                                    context="task") is True
    # auto_all notwithstanding: dangerous is never auto outside interactive.
    assert permissions.auto_approve(conn, "shell_command", level="dangerous",
                                    context="task") is False


def test_channel_follow_caps_shell_at_gray():
    conn = _conn()
    permissions.save(conn, {"gates": {"shell": "auto_all"},
                            "contexts": {"channels": "follow"}})
    assert permissions.auto_approve(conn, "shell_command", level="gray",
                                    context="channel") is True
    assert permissions.auto_approve(conn, "shell_command", level="dangerous",
                                    context="channel") is False


def test_run_context_var_is_honored():
    conn = _conn()
    permissions.save(conn, {"gates": {"notes": "auto"}})
    token = permissions.RUN_CONTEXT_VAR.set("task")
    try:
        assert permissions.auto_approve(conn, "notes_write") is False
    finally:
        permissions.RUN_CONTEXT_VAR.reset(token)
    assert permissions.auto_approve(conn, "notes_write") is True


def test_auto_approve_fails_closed_on_broken_conn():
    class Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db down")
    permissions.save(_conn(), {"gates": {"notes": "auto"}})  # unrelated conn
    assert permissions.auto_approve(Boom(), "notes_write") is False


# -- judges / helpers ---------------------------------------------------------

def test_judge_enabled_defaults_and_toggle():
    conn = _conn()
    assert permissions.judge_enabled(conn, "shell") is True
    assert permissions.judge_enabled(conn, "egress") is True
    permissions.save(conn, {"judges": {"shell": False}})
    assert permissions.judge_enabled(conn, "shell") is False
    assert permissions.judge_enabled(conn, "egress") is True
    assert permissions.judge_enabled(conn, "unknown") is True


def test_context_mode_and_proxy_settings():
    conn = _conn()
    assert permissions.context_mode(conn, "tasks") == "strict"
    permissions.save(conn, {"contexts": {"channels": "auto"},
                            "proxy": {"upload_threshold_kb": 512}})
    assert permissions.context_mode(conn, "channels") == "auto"
    assert permissions.proxy_settings(conn) == {
        "tofu_ttl_hours": 1, "upload_threshold_kb": 512}


def test_stored_document_is_normalized_json():
    conn = _conn()
    permissions.save(conn, {"gates": {"apps": "auto"}})
    row = conn.execute(
        "SELECT value FROM user_settings WHERE user_id='__global__' "
        "AND key='permission_policy'").fetchone()
    doc = json.loads(row["value"])
    assert doc["gates"]["apps"] == "auto"
    assert set(doc) == {"preset", "gates", "judges", "contexts", "proxy"}
