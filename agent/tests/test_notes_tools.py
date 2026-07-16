import asyncio
import json

import pytest

import db as db_module
from notes import store
from skills import notes as notes_skills


class _AutoConfirm:
    """register→wait 直通的假 ConfirmManager。approve=False 模拟用户拒绝。"""
    def __init__(self, approve=True):
        self.approve = approve
        self.registered = []

    def register(self, session_id, action, description, command):
        self.registered.append((action, description))
        return "cid-1"

    async def wait(self, confirm_id):
        return self.approve


class _Sink:
    def __init__(self):
        self.events = []

    async def put(self, ev):
        self.events.append(ev)


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    c = db_module.init_db(str(tmp_path / "t.db"),
                          snapshots_root=str(tmp_path / "snaps"))
    store.set_notes_root(c, str(tmp_path / "Notes"))
    monkeypatch.setattr(db_module, "get_connection", lambda: c)

    async def _fake_index(note, body):
        return True
    monkeypatch.setattr(notes_skills, "index_note", _fake_index)
    return c


def _ctx(approve=True):
    notes_skills.USER_ID_VAR.set("1")
    notes_skills.SESSION_ID_VAR.set("s1")
    notes_skills.CONFIRM_MGR_VAR.set(_AutoConfirm(approve))
    notes_skills.EVENT_QUEUE_VAR.set(_Sink())


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_write_note_confirmed_creates_curated(conn):
    _ctx(approve=True)
    out = json.loads(_run(notes_skills._write_note_impl(
        "T", "content", "note", ["tag1"], [])))
    assert out["ok"] is True and out["status"] == "curated"
    rows = store.list_notes(conn, "1")
    assert rows and rows[0]["created_by"] == "agent"


def test_write_note_declined_writes_nothing(conn):
    _ctx(approve=False)
    out = json.loads(_run(notes_skills._write_note_impl(
        "T", "content", "note", [], [])))
    assert out.get("error") == "user declined"
    assert store.list_notes(conn, "1") == []


def test_write_note_rejects_bad_type(conn):
    _ctx()
    out = json.loads(_run(notes_skills._write_note_impl(
        "T", "c", "banana", [], [])))
    assert "invalid type" in out.get("error", "")


def test_update_note_conflict_reports_revision(conn):
    _ctx()
    n = store.create_note(conn, "1", title="T", body="v1")
    out = json.loads(_run(notes_skills._update_note_impl(
        n["id"], expected_revision=99, content="v2")))
    assert out.get("error") == "revision conflict"
    assert out["current_revision"] == 1


def test_read_and_list_are_user_scoped(conn):
    _ctx()
    n = store.create_note(conn, "2", title="other", body="secret")
    out = json.loads(_run(notes_skills._read_note_impl(n["id"])))
    assert "error" in out                       # 别人的笔记读不到
    out2 = json.loads(_run(notes_skills._list_notes_impl("", "", 20)))
    assert out2["notes"] == []


def test_no_user_context_refuses(conn):
    notes_skills.USER_ID_VAR.set("")
    out = json.loads(_run(notes_skills._list_notes_impl("", "", 20)))
    assert "error" in out


def test_registry_contains_notes_category():
    from skills.tool_registry import CATEGORY_TOOLS, CATEGORY_DESCRIPTIONS
    assert "notes" in CATEGORY_TOOLS and "notes" in CATEGORY_DESCRIPTIONS
    names = {getattr(t, "name", getattr(t, "__name__", ""))
             for t in CATEGORY_TOOLS["notes"]}
    assert {"write_note", "update_note", "read_note",
            "list_notes"} <= names
