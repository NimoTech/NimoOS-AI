"""escalate — forward an out-of-scope confirmation card to the task's
paired channel. Everything injected: no main import, no real adapter."""
import json
import sqlite3

import pytest

from tasks import escalate


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE channel_instances(id TEXT PRIMARY KEY, channel_type TEXT);
        CREATE TABLE channel_chats(instance_id TEXT, external_chat_id TEXT,
                                   binding_id TEXT);
        CREATE TABLE channel_bindings(id TEXT PRIMARY KEY, user_id TEXT,
                                      revoked INTEGER DEFAULT 0);
        CREATE TABLE scheduled_tasks(id TEXT PRIMARY KEY, user_id TEXT,
            name TEXT, preauth_json TEXT DEFAULT '{}',
            updated_at INTEGER DEFAULT 0);
    """)
    conn.execute("INSERT INTO channel_instances VALUES ('inst1','telegram')")
    conn.execute("INSERT INTO channel_bindings(id,user_id) VALUES ('b1','u1')")
    conn.execute("INSERT INTO channel_chats VALUES ('inst1','chat1','b1')")
    conn.execute("INSERT INTO scheduled_tasks(id,user_id,name) "
                 "VALUES ('t1','u1','Daily')")
    conn.commit()
    return conn


def _task_row(conn, channel="inst1:chat1"):
    return conn.execute(
        "SELECT id,user_id,name,preauth_json,? AS notify_channel "
        "FROM scheduled_tasks WHERE id='t1'", (channel,)).fetchone()


class FakeMgr:
    def __init__(self):
        self.resolved = []

    def resolve(self, cid, confirmed, expected_session_id=None, source=None):
        self.resolved.append((cid, confirmed, source))


class FakeRouter:
    def __init__(self, ok=True):
        self._ok = ok
        self.calls = []

    async def surface_external_confirm(self, adapter, chat_id, session_id,
                                       confirm_id, text, *, timeout=None,
                                       persist_label=None, on_resolved=None):
        self.calls.append({"chat_id": chat_id, "session_id": session_id,
                           "confirm_id": confirm_id, "text": text,
                           "timeout": timeout, "persist_label": persist_label,
                           "on_resolved": on_resolved})
        return self._ok


class FakeAdapter:
    class capabilities:
        supports_buttons = True


class NoButtonsAdapter:
    class capabilities:
        supports_buttons = False


class FakeManager:
    def __init__(self, router, adapter):
        self._router = router
        self._running = {"inst1": (adapter, "fp")}


def _egress_ev(cid="c1", host="x.com"):
    return {"type": "confirmation_required", "confirm_id": cid,
            "action": "egress_confirm", "host": host}


def test_build_returns_none_without_channel():
    conn = _conn()
    row = _task_row(conn, channel="")
    assert escalate.build(conn, row, session_id="s1",
                          confirm_mgr=FakeMgr()) is None


@pytest.mark.asyncio
async def test_escalates_to_router_with_persist_button():
    conn = _conn()
    router = FakeRouter()
    manager = FakeManager(router, FakeAdapter())
    esc = escalate.build(conn, _task_row(conn), session_id="s1",
                         confirm_mgr=FakeMgr(),
                         get_manager=lambda: manager)
    outcomes = []
    await esc(_egress_ev(), lambda a, p: outcomes.append((a, p)))
    call, = router.calls
    assert call["confirm_id"] == "c1" and call["chat_id"] == "chat1"
    assert call["session_id"] == "s1"
    assert "Daily" in call["text"] and "x.com" in call["text"]
    assert call["persist_label"]
    assert call["timeout"] and call["timeout"] > 0
    assert outcomes == []          # no verdict until a click arrives
    # simulate the "allow & persist" click
    call["on_resolved"](True, True)
    assert outcomes == [(True, True)]
    doc = json.loads(conn.execute(
        "SELECT preauth_json FROM scheduled_tasks WHERE id='t1'"
    ).fetchone()[0])
    assert "x.com" in doc["egress_domains"]


@pytest.mark.asyncio
async def test_allow_once_does_not_persist():
    conn = _conn()
    router = FakeRouter()
    manager = FakeManager(router, FakeAdapter())
    esc = escalate.build(conn, _task_row(conn), session_id="s1",
                         confirm_mgr=FakeMgr(),
                         get_manager=lambda: manager)
    outcomes = []
    await esc(_egress_ev(), lambda a, p: outcomes.append((a, p)))
    router.calls[0]["on_resolved"](True, False)
    assert outcomes == [(True, False)]
    doc = json.loads(conn.execute(
        "SELECT preauth_json FROM scheduled_tasks WHERE id='t1'"
    ).fetchone()[0])
    assert doc.get("egress_domains", []) == []


@pytest.mark.asyncio
async def test_no_running_adapter_denies_immediately():
    conn = _conn()
    mgr = FakeMgr()
    manager = FakeManager(FakeRouter(), FakeAdapter())
    manager._running = {}
    esc = escalate.build(conn, _task_row(conn), session_id="s1",
                         confirm_mgr=mgr, get_manager=lambda: manager)
    outcomes = []
    await esc(_egress_ev(), lambda a, p: outcomes.append((a, p)))
    assert mgr.resolved == [("c1", False, "task-driver")]
    assert outcomes == [(False, False)]


@pytest.mark.asyncio
async def test_buttonless_adapter_denies():
    conn = _conn()
    mgr = FakeMgr()
    manager = FakeManager(FakeRouter(), NoButtonsAdapter())
    esc = escalate.build(conn, _task_row(conn), session_id="s1",
                         confirm_mgr=mgr, get_manager=lambda: manager)
    outcomes = []
    await esc(_egress_ev(), lambda a, p: outcomes.append((a, p)))
    assert mgr.resolved == [("c1", False, "task-driver")]
    assert outcomes == [(False, False)]


@pytest.mark.asyncio
async def test_unpaired_chat_denies():
    conn = _conn()
    mgr = FakeMgr()
    manager = FakeManager(FakeRouter(), FakeAdapter())
    esc = escalate.build(conn, _task_row(conn, channel="inst1:otherchat"),
                         session_id="s1", confirm_mgr=mgr,
                         get_manager=lambda: manager)
    outcomes = []
    await esc(_egress_ev(), lambda a, p: outcomes.append((a, p)))
    assert mgr.resolved == [("c1", False, "task-driver")]
    assert outcomes == [(False, False)]


@pytest.mark.asyncio
async def test_send_failure_denies():
    conn = _conn()
    mgr = FakeMgr()
    manager = FakeManager(FakeRouter(ok=False), FakeAdapter())
    esc = escalate.build(conn, _task_row(conn), session_id="s1",
                         confirm_mgr=mgr, get_manager=lambda: manager)
    outcomes = []
    await esc(_egress_ev(), lambda a, p: outcomes.append((a, p)))
    assert mgr.resolved == [("c1", False, "task-driver")]
    assert outcomes == [(False, False)]


@pytest.mark.asyncio
async def test_persist_fold_failure_still_reports_allow():
    # Folding /etc is refused — the one-off allow already resolved must
    # survive; only the persistence is skipped.
    conn = _conn()
    router = FakeRouter()
    manager = FakeManager(router, FakeAdapter())
    esc = escalate.build(conn, _task_row(conn), session_id="s1",
                         confirm_mgr=FakeMgr(),
                         get_manager=lambda: manager)
    outcomes = []
    await esc({"confirm_id": "c1", "type": "access_request",
               "path": "/etc/passwd"}, lambda a, p: outcomes.append((a, p)))
    router.calls[0]["on_resolved"](True, True)
    assert outcomes == [(True, True)]
    doc = json.loads(conn.execute(
        "SELECT preauth_json FROM scheduled_tasks WHERE id='t1'"
    ).fetchone()[0])
    assert doc.get("fs_write", []) == []


@pytest.mark.asyncio
async def test_missing_confirm_id_reports_deny_without_resolve():
    conn = _conn()
    mgr = FakeMgr()
    manager = FakeManager(FakeRouter(), FakeAdapter())
    esc = escalate.build(conn, _task_row(conn), session_id="s1",
                         confirm_mgr=mgr, get_manager=lambda: manager)
    outcomes = []
    await esc({"type": "confirmation_required", "action": "egress_confirm",
               "host": "x.com"}, lambda a, p: outcomes.append((a, p)))
    assert mgr.resolved == []
    assert outcomes == [(False, False)]
