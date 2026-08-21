"""Permission-policy wiring: gates consult permissions.py before raising a
card — shell levels + judge toggle, fs auto-grant, task driver contexts,
channel router, egress-confirm route, proxy argv, settings endpoints."""
from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

import db as dbmod
import permissions
from skills import shell


class _Mgr:
    def __init__(self, grant=True, remember=False):
        self._grant, self._remember = grant, remember
        self.registered = []
        self.resolved = []

    def register(self, sid, action, desc, command):
        self.registered.append((action, command))
        return f"cid-{len(self.registered)}"

    async def wait(self, cid):
        return self._grant

    def consume_remember(self, cid):
        return self._remember

    def resolve(self, cid, confirmed, remember=False,
                expected_session_id=None, **kw):
        self.resolved.append((cid, confirmed))


class _Sink:
    def __init__(self):
        self.events = []

    async def put(self, ev):
        self.events.append(ev)


def _setup_shell(monkeypatch, mgr, sink):
    conn = dbmod.init_db(":memory:")
    conn.execute("INSERT INTO sessions (id,user_id,created_at,updated_at) "
                 "VALUES ('s1','u1',0,0)")
    conn.commit()
    shell.SESSION_ID_VAR.set("s1")
    shell.DB_VAR.set(conn)
    shell.CONFIRM_MGR_VAR.set(mgr)
    shell.EVENT_QUEUE_VAR.set(sink)
    monkeypatch.setattr(
        "shell_guard.backstop.prepare_backstop",
        lambda paths, trash_root=None: __import__(
            "shell_guard.backstop", fromlist=["BackstopResult"]
        ).BackstopResult("none", "", False, ""))
    return conn


@pytest.fixture(autouse=True)
def _interactive_context():
    token = permissions.RUN_CONTEXT_VAR.set("interactive")
    yield
    permissions.RUN_CONTEXT_VAR.reset(token)


# -- shell gate ---------------------------------------------------------------

def test_shell_auto_gray_skips_judge_and_card(monkeypatch):
    mgr, sink = _Mgr(), _Sink()
    conn = _setup_shell(monkeypatch, mgr, sink)
    permissions.save(conn, {"gates": {"shell": "auto_gray"}})

    async def _boom(_cmd):
        raise AssertionError("judge must not be called under auto_gray")
    monkeypatch.setattr(shell, "judge_command", _boom)

    # `cryptsetup status foo` is gray (unknown-ish admin tool, no paths).
    assert asyncio.run(shell._guard_command("cryptsetup status foo")) is None
    assert mgr.registered == []


def test_shell_auto_gray_still_cards_dangerous(monkeypatch):
    mgr, sink = _Mgr(grant=False), _Sink()
    conn = _setup_shell(monkeypatch, mgr, sink)
    permissions.save(conn, {"gates": {"shell": "auto_gray"}})
    msg = asyncio.run(shell._guard_command("rm -rf /DATA/x"))
    assert msg is not None
    assert mgr.registered and mgr.registered[0][0] == "shell_command"


def test_shell_auto_all_waives_dangerous_but_never_protected(monkeypatch):
    mgr, sink = _Mgr(grant=False), _Sink()
    conn = _setup_shell(monkeypatch, mgr, sink)
    permissions.save(conn, {"gates": {"shell": "auto_all"}})
    assert asyncio.run(shell._guard_command("rm -rf /DATA/x")) is None
    assert mgr.registered == []
    # protected: mass delete of /etc — always a card, policy or not
    msg = asyncio.run(shell._guard_command("rm -rf /etc"))
    assert msg is not None
    assert mgr.registered and mgr.registered[0][0] == "shell_command"


def test_shell_task_context_caps_auto_all_at_gray(monkeypatch):
    mgr, sink = _Mgr(grant=False), _Sink()
    conn = _setup_shell(monkeypatch, mgr, sink)
    permissions.save(conn, {"gates": {"shell": "auto_all"},
                            "contexts": {"tasks": "follow"}})
    token = permissions.RUN_CONTEXT_VAR.set("task")
    try:
        msg = asyncio.run(shell._guard_command("rm -rf /DATA/x"))
        assert msg is not None            # dangerous not waived in a task run
    finally:
        permissions.RUN_CONTEXT_VAR.reset(token)


def test_shell_judge_disabled_goes_straight_to_card(monkeypatch):
    mgr, sink = _Mgr(grant=True), _Sink()
    conn = _setup_shell(monkeypatch, mgr, sink)
    permissions.save(conn, {"judges": {"shell": False}})

    async def _boom(_cmd):
        raise AssertionError("judge must not be called when disabled")
    monkeypatch.setattr(shell, "judge_command", _boom)

    assert asyncio.run(shell._guard_command("cryptsetup status foo")) is None
    assert mgr.registered and mgr.registered[0][0] == "shell_command"


def test_shell_judge_emits_judging_and_judged_events(monkeypatch):
    """The user must SEE the judge working: `judging` starts the wait
    indicator, `judged` ends it with the verdict."""
    mgr, sink = _Mgr(grant=True), _Sink()
    _setup_shell(monkeypatch, mgr, sink)

    async def _judge(_cmd):
        return "allow"
    monkeypatch.setattr(shell, "judge_command", _judge)
    assert asyncio.run(shell._guard_command("cryptsetup status foo")) is None
    types = [(e.get("type"), e.get("kind"), e.get("verdict")) for e in sink.events]
    assert ("judging", "shell", None) in types
    assert ("judged", "shell", "allow") in types
    assert mgr.registered == []          # allow verdict → no card, no click


def test_shell_judge_events_precede_the_card_on_ask(monkeypatch):
    mgr, sink = _Mgr(grant=True), _Sink()
    _setup_shell(monkeypatch, mgr, sink)

    async def _judge(_cmd):
        return "ask"
    monkeypatch.setattr(shell, "judge_command", _judge)
    asyncio.run(shell._guard_command("cryptsetup status foo"))
    types = [e.get("type") for e in sink.events]
    assert types.index("judging") < types.index("judged") \
        < types.index("confirmation_required")


def test_shell_judge_disabled_emits_no_judging_events(monkeypatch):
    mgr, sink = _Mgr(grant=True), _Sink()
    conn = _setup_shell(monkeypatch, mgr, sink)
    permissions.save(conn, {"judges": {"shell": False}})

    async def _boom(_cmd):
        raise AssertionError("judge must not run")
    monkeypatch.setattr(shell, "judge_command", _boom)
    asyncio.run(shell._guard_command("cryptsetup status foo"))
    assert all(e.get("type") != "judging" for e in sink.events)


def test_shell_default_policy_unchanged(monkeypatch):
    """No policy stored → the pre-existing judge path runs."""
    mgr, sink = _Mgr(grant=True), _Sink()
    _setup_shell(monkeypatch, mgr, sink)
    calls = []

    async def _judge(cmd):
        calls.append(cmd)
        return "allow"
    monkeypatch.setattr(shell, "judge_command", _judge)
    assert asyncio.run(shell._guard_command("cryptsetup status foo")) is None
    assert calls == ["cryptsetup status foo"]


# -- fs access ---------------------------------------------------------------

def _fs_ctx(conn, mgr, sink):
    conn.execute("INSERT OR IGNORE INTO sessions (id,user_id,created_at,"
                 "updated_at) VALUES ('s1','u1',0,0)")
    conn.commit()
    return {"conn": conn, "session_id": "s1", "run_id": "",
            "confirm_mgr": mgr, "sink": sink}


def test_fs_auto_grant_persists_and_skips_card(tmp_path):
    from fs import access_request as ar
    ar.reset_state()
    conn = dbmod.init_db(":memory:")
    permissions.save(conn, {"gates": {"fs_access": "auto"}})
    mgr, sink = _Mgr(), _Sink()
    target = str(tmp_path / "folder")
    os.makedirs(target)
    granted = asyncio.run(ar.request_access(
        _fs_ctx(conn, mgr, sink), target, "folder", "list"))
    assert granted is True
    assert mgr.registered == [] and sink.events == []
    row = conn.execute("SELECT path FROM visible_resources "
                       "WHERE session_id='s1'").fetchone()
    assert row["path"] == target
    req = conn.execute("SELECT decision FROM access_requests").fetchone()
    assert req["decision"] == "granted"


def test_fs_auto_never_grants_system_locations():
    from fs import access_request as ar
    ar.reset_state()
    conn = dbmod.init_db(":memory:")
    permissions.save(conn, {"gates": {"fs_access": "auto"}})
    mgr, sink = _Mgr(grant=False), _Sink()
    granted = asyncio.run(ar.request_access(
        _fs_ctx(conn, mgr, sink), "/usr/share/x", "folder", "list"))
    assert granted is False
    assert mgr.registered          # fell through to the card
    assert sink.events and sink.events[0]["type"] == "access_request"


def test_fs_batch_auto_grants_every_path(tmp_path):
    from fs import access_request as ar
    ar.reset_state()
    conn = dbmod.init_db(":memory:")
    permissions.save(conn, {"gates": {"fs_access": "auto"}})
    mgr, sink = _Mgr(), _Sink()
    p1, p2 = str(tmp_path / "a"), str(tmp_path / "b")
    os.makedirs(p1), os.makedirs(p2)
    granted = asyncio.run(ar.request_access_batch(
        _fs_ctx(conn, mgr, sink), [p1, p2], "write"))
    assert granted is True and sink.events == []
    rows = conn.execute("SELECT path FROM visible_resources").fetchall()
    assert {r["path"] for r in rows} == {p1, p2}


def test_fs_default_policy_still_cards(tmp_path):
    from fs import access_request as ar
    ar.reset_state()
    conn = dbmod.init_db(":memory:")
    mgr, sink = _Mgr(grant=True), _Sink()
    target = str(tmp_path / "c")
    os.makedirs(target)
    granted = asyncio.run(ar.request_access(
        _fs_ctx(conn, mgr, sink), target, "folder", "list"))
    assert granted is True
    assert sink.events and sink.events[0]["type"] == "access_request"


# -- task driver ---------------------------------------------------------------

def _drive_one(ev, policy, preauth=None):
    from tasks.driver import TaskRunDriver
    mgr = _Mgr()
    d = TaskRunDriver(confirm_mgr=mgr, session_id="s1",
                      preauth=preauth or {}, run_timeout=5, policy=policy)
    d._handle_confirm(ev)
    return mgr, d


def _policy(tasks_mode, **gates):
    doc = permissions.default_policy()
    doc["contexts"]["tasks"] = tasks_mode
    doc["gates"].update(gates)
    return doc


def test_driver_strict_denies_like_today():
    ev = {"type": "confirmation_required", "action": "egress_confirm",
          "confirm_id": "c1", "host": "example.com"}
    mgr, d = _drive_one(ev, _policy("strict"))
    assert mgr.resolved == [("c1", False)]
    assert d._denied and not d._auto_approved


def test_driver_follow_approves_egress_when_network_auto():
    ev = {"type": "confirmation_required", "action": "egress_confirm",
          "confirm_id": "c1", "host": "example.com",
          "reason": "tofu_unknown_host"}
    mgr, d = _drive_one(ev, _policy("follow", network="auto"))
    assert mgr.resolved == [("c1", True)]
    assert d._auto_approved


def test_driver_follow_upload_needs_the_upload_gate():
    ev = {"type": "confirmation_required", "action": "egress_confirm",
          "confirm_id": "c1", "host": "example.com",
          "reason": "upload_over_threshold"}
    mgr, _ = _drive_one(ev, _policy("follow", network="auto"))
    assert mgr.resolved == [("c1", False)]
    mgr, _ = _drive_one(ev, _policy("follow", upload="auto"))
    assert mgr.resolved == [("c1", True)]


def test_driver_auto_approves_mcp_but_never_shell():
    ev = {"type": "confirmation_required", "action": "mcp_call:s::t",
          "confirm_id": "c1"}
    mgr, _ = _drive_one(ev, _policy("auto"))
    assert mgr.resolved == [("c1", True)]
    ev2 = {"type": "confirmation_required", "action": "shell_command",
           "confirm_id": "c2", "command": "rm -rf /DATA/x"}
    mgr, _ = _drive_one(ev2, _policy("auto"))
    assert mgr.resolved == [("c2", False)]


def test_driver_auto_fs_respects_deny_roots(tmp_path):
    ok = str(tmp_path / "ok")
    os.makedirs(ok)
    ev = {"type": "access_request", "confirm_id": "c1", "path": ok}
    mgr, _ = _drive_one(ev, _policy("auto"))
    assert mgr.resolved == [("c1", True)]
    ev2 = {"type": "access_request", "confirm_id": "c2", "path": "/etc/ssh"}
    mgr, _ = _drive_one(ev2, _policy("auto"))
    assert mgr.resolved == [("c2", False)]


def test_driver_auto_never_approves_elicitation():
    """contexts.tasks=auto must not blanket-approve card kinds that have no
    gate — MCP elicitation needs a real answer, and 'approved with no content'
    is a lie in the audit trail."""
    ev = {"type": "confirmation_required", "confirm_id": "c1",
          "kind": "mcp_elicit_url", "url": "https://srv.example/authorize"}
    mgr, d = _drive_one(ev, _policy("auto"))
    assert mgr.resolved == [("c1", False)]
    ev2 = {"type": "confirmation_required", "confirm_id": "c2",
           "kind": "mcp_elicit_form", "server": "srv"}
    mgr, _ = _drive_one(ev2, _policy("auto"))
    assert mgr.resolved == [("c2", False)]


def test_driver_follow_maps_kind_only_mcp_events():
    """Real mcp_tool/mcp_install/toolbox_install events carry `kind`, not
    `action` — follow mode must honor the gate for them."""
    ev = {"type": "confirmation_required", "confirm_id": "c1",
          "kind": "mcp_tool", "server": "srv", "tool": "t"}
    mgr, _ = _drive_one(ev, _policy("follow", mcp_tools="auto"))
    assert mgr.resolved == [("c1", True)]
    ev2 = {"type": "confirmation_required", "confirm_id": "c2",
           "kind": "toolbox_install", "title": "Install gh"}
    mgr, _ = _drive_one(ev2, _policy("follow", installs="auto"))
    assert mgr.resolved == [("c2", True)]
    mgr, _ = _drive_one(ev2, _policy("follow"))
    assert mgr.resolved == [("c2", False)]


def test_driver_resolves_with_non_user_source():
    ev = {"type": "confirmation_required", "action": "egress_confirm",
          "confirm_id": "c1", "host": "api.example.com",
          "reason": "tofu_unknown_host"}
    from tasks.driver import TaskRunDriver
    sources = []

    class _SrcMgr(_Mgr):
        def resolve(self, cid, confirmed, remember=False,
                    expected_session_id=None, source="user", **kw):
            sources.append(source)
    d = TaskRunDriver(confirm_mgr=_SrcMgr(), session_id="s1",
                      preauth={"egress_domains": ["api.example.com"]},
                      run_timeout=5, policy=None)
    d._handle_confirm(ev)
    assert sources == ["task-driver"]


def test_driver_none_policy_is_strict():
    ev = {"type": "confirmation_required", "action": "mcp_call:s::t",
          "confirm_id": "c1"}
    mgr, _ = _drive_one(ev, None)
    assert mgr.resolved == [("c1", False)]


def test_driver_preauth_still_wins_over_policy():
    ev = {"type": "confirmation_required", "action": "egress_confirm",
          "confirm_id": "c1", "host": "api.example.com"}
    mgr, d = _drive_one(ev, _policy("strict"),
                        preauth={"egress_domains": ["api.example.com"]})
    assert mgr.resolved == [("c1", True)]


# -- channel router -----------------------------------------------------------

def test_channel_router_auto_resolves_egress_card():
    from channels.router import ChannelRouter
    conn = dbmod.init_db(":memory:")
    permissions.save(conn, {"contexts": {"channels": "auto"}})
    resolved = []

    def _resolve(cid, ok, expected_session_id=None, source="user"):
        resolved.append((cid, ok, source))
    r = ChannelRouter(conn, start_run=None, cancel_run=None,
                      resolve_credentials=None, resolve_confirm=_resolve)
    ev = {"type": "confirmation_required", "confirm_id": "c1",
          "action": "egress_confirm", "reason": "tofu_unknown_host",
          "host": "example.com"}
    asyncio.run(r._surface_confirm(object(), "chat1", "s1", ev))
    # source="policy": the audit must not claim a human pressed Allow
    assert resolved == [("c1", True, "policy")]


def test_channel_router_strict_keeps_buttons_path():
    from channels.router import ChannelRouter
    conn = dbmod.init_db(":memory:")
    resolved = []

    def _resolve(cid, ok, expected_session_id=None, source="user"):
        resolved.append((cid, ok))

    class _NoButtons:
        instance_id = "i1"
        class capabilities:  # noqa: D106
            supports_buttons = False
    r = ChannelRouter(conn, start_run=None, cancel_run=None,
                      resolve_credentials=None, resolve_confirm=_resolve)
    ev = {"type": "confirmation_required", "confirm_id": "c1",
          "action": "egress_confirm", "reason": "tofu_unknown_host"}
    asyncio.run(r._surface_confirm(_NoButtons(), "chat1", "s1", ev))
    # default policy → not auto-approved → no-buttons adapter denies
    assert resolved == [("c1", False)]


def test_channel_router_never_auto_grants_system_fs_paths():
    from channels.router import ChannelRouter
    conn = dbmod.init_db(":memory:")
    permissions.save(conn, {"contexts": {"channels": "auto"}})
    resolved = []

    def _resolve(cid, ok, expected_session_id=None):
        resolved.append((cid, ok))

    class _NoButtons:
        instance_id = "i1"
        class capabilities:  # noqa: D106
            supports_buttons = False
    r = ChannelRouter(conn, start_run=None, cancel_run=None,
                      resolve_credentials=None, resolve_confirm=_resolve)
    ev = {"type": "access_request", "confirm_id": "c1", "path": "/etc/ssh"}
    asyncio.run(r._surface_confirm(_NoButtons(), "chat1", "s1", ev))
    assert resolved == [("c1", False)]      # denied via the buttons path


def test_channel_router_never_auto_approves_elicitation():
    from channels.router import ChannelRouter
    conn = dbmod.init_db(":memory:")
    permissions.save(conn, {"contexts": {"channels": "auto"}})
    resolved = []

    def _resolve(cid, ok, expected_session_id=None, source="user"):
        resolved.append((cid, ok))

    class _NoButtons:
        instance_id = "i1"
        class capabilities:  # noqa: D106
            supports_buttons = False
    r = ChannelRouter(conn, start_run=None, cancel_run=None,
                      resolve_credentials=None, resolve_confirm=_resolve)
    ev = {"type": "confirmation_required", "confirm_id": "c1",
          "kind": "mcp_elicit_form", "server": "srv"}
    asyncio.run(r._surface_confirm(_NoButtons(), "chat1", "s1", ev))
    assert resolved == [("c1", False)]      # never auto, falls to deny path


# -- proxy argv + endpoints -----------------------------------------------------

def test_build_proxy_argv_default_policy_is_byte_identical():
    import main as main_mod
    base = main_mod._build_proxy_argv("/bin/proxy")
    with_defaults = main_mod._build_proxy_argv(
        "/bin/proxy", tofu_ttl_hours=1, upload_threshold_kb=64)
    assert base == with_defaults
    assert "-tofu-ttl" not in base


def test_build_proxy_argv_policy_overrides():
    import main as main_mod
    argv = main_mod._build_proxy_argv(
        "/bin/proxy", tofu_ttl_hours=24, upload_threshold_kb=512)
    assert argv[argv.index("-tofu-ttl") + 1] == "24h"
    assert argv[argv.index("-upload-threshold") + 1] == str(512 * 1024)


@pytest.fixture
def client():
    import main as main_mod
    return TestClient(main_mod.app)


_H = {"X-User-Id": "u1"}


def test_permission_settings_get_defaults(client):
    r = client.get("/agent/permission-settings", headers=_H)
    assert r.status_code == 200
    assert r.json() == permissions.default_policy()


def test_permission_settings_put_normalizes_and_persists(client):
    import main as main_mod
    try:
        r = client.put("/agent/permission-settings", headers=_H, json={
            "gates": {"notes": "auto", "shell": "sudo-everything"},
            "contexts": {"tasks": "follow"}})
        assert r.status_code == 200
        body = r.json()
        assert body["gates"]["notes"] == "auto"
        assert body["gates"]["shell"] == "ask"          # invalid → default
        assert body["contexts"]["tasks"] == "follow"
        assert permissions.load(main_mod._db()) == body
    finally:
        permissions.save(main_mod._db(), permissions.default_policy())


def test_permission_settings_put_rejects_non_object(client):
    r = client.put("/agent/permission-settings", headers=_H, json=[1, 2])
    assert r.status_code == 400


def test_egress_confirm_route_no_session_denies_even_with_auto_policy(client):
    """The no-active-session fail-closed must survive the policy: a grant
    with nobody to attribute it to is not a grant."""
    import main as main_mod
    permissions.save(main_mod._db(), {"gates": {"network": "auto"}})
    try:
        main_mod._runner._active_sinks.clear()
        main_mod._runner._last_active_session = None
        r = client.post("/internal/egress-confirm", json={
            "host": "unknown.example.com", "bytes": 0,
            "reason": "tofu_unknown_host"})
        assert r.json() == {"allow": False}
    finally:
        permissions.save(main_mod._db(), permissions.default_policy())


def test_egress_confirm_route_auto_approves_tofu_for_interactive_session(client):
    import main as main_mod
    permissions.save(main_mod._db(), {"gates": {"network": "auto"}})
    sink = _Sink()
    main_mod._runner._active_sinks["perm-s1"] = sink
    main_mod._runner._last_active_session = "perm-s1"
    main_mod._runner._run_contexts["perm-s1"] = "interactive"
    try:
        r = client.post("/internal/egress-confirm", json={
            "host": "unknown.example.com", "bytes": 0,
            "reason": "tofu_unknown_host"})
        assert r.json() == {"allow": True}
        assert sink.events == []            # no card was raised
        # the upload gate is separate and stays closed
        # (falls to the card path; the fake mgr below denies immediately)
    finally:
        main_mod._runner._active_sinks.pop("perm-s1", None)
        main_mod._runner._run_contexts.pop("perm-s1", None)
        main_mod._runner._last_active_session = None
        permissions.save(main_mod._db(), permissions.default_policy())


def test_egress_confirm_route_respects_strict_task_context(client, monkeypatch):
    """gates.network=auto for interactive convenience must NOT open the TOFU
    gate for a scheduled run whose contexts.tasks is strict — the card must
    still be raised into the session sink (where TaskRunDriver answers from
    preauth)."""
    import main as main_mod
    permissions.save(main_mod._db(), {"gates": {"network": "auto"}})

    class _FakeMgr:
        def register(self, sid, action, desc, command):
            return "cid-task"

        async def wait(self, cid, timeout=None):
            return False
    sink = _Sink()
    main_mod._runner._active_sinks["perm-t1"] = sink
    main_mod._runner._last_active_session = "perm-t1"
    main_mod._runner._run_contexts["perm-t1"] = "task"
    monkeypatch.setattr(main_mod, "_confirm_mgr", _FakeMgr())
    try:
        r = client.post("/internal/egress-confirm", json={
            "host": "attacker.example.com", "bytes": 0,
            "reason": "tofu_unknown_host"})
        assert r.json() == {"allow": False}
        assert sink.events and sink.events[0]["action"] == "egress_confirm"
    finally:
        main_mod._runner._active_sinks.pop("perm-t1", None)
        main_mod._runner._run_contexts.pop("perm-t1", None)
        main_mod._runner._last_active_session = None
        permissions.save(main_mod._db(), permissions.default_policy())
