import asyncio
import os
import sqlite3

import pytest

import db as db_module
from notes import store, sync
from notes.okf import parse_note_text, serialize_note_text


class _Conn(sqlite3.Connection):
    """sqlite3.Connection has no __dict__; subclass so the fixture below
    can stash `_test_index_calls` on the connection object itself."""


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    _orig_connect = sqlite3.connect

    def _connect(*a, **kw):
        kw.setdefault("factory", _Conn)
        return _orig_connect(*a, **kw)

    monkeypatch.setattr(sqlite3, "connect", _connect)
    c = db_module.init_db(str(tmp_path / "t.db"),
                          snapshots_root=str(tmp_path / "snaps"))
    store.set_notes_root(c, str(tmp_path / "Notes"))
    calls = []

    async def _fake_index(note, body):
        calls.append(("index", note["id"]))
        return True

    async def _fake_deindex(user_id, note_id):
        calls.append(("deindex", note_id))
        return True

    monkeypatch.setattr(sync, "index_note", _fake_index)
    monkeypatch.setattr(sync, "deindex_note", _fake_deindex)
    c._test_index_calls = calls
    return c


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_adopts_file_without_id(conn):
    root = store.get_notes_root(conn)
    os.makedirs(f"{root}/1", exist_ok=True)
    with open(f"{root}/1/manual.md", "w") as f:
        f.write("just some hand-written text\n")
    stats = _run(sync.scan_once(conn))
    assert stats["adopted"] == 1
    with open(f"{root}/1/manual.md") as f:
        meta, _ = parse_note_text(f.read())
    assert meta["id"]                       # UUID 已回写
    row = conn.execute("SELECT * FROM notes WHERE id=?",
                       (meta["id"],)).fetchone()
    assert row["user_id"] == "1" and row["created_by"] == "human"
    assert row["status"] == "curated"
    assert ("index", meta["id"]) in conn._test_index_calls


def test_own_write_is_echo_suppressed(conn):
    store.create_note(conn, "1", title="t", body="b")
    stats = _run(sync.scan_once(conn))
    assert stats == {"adopted": 0, "updated": 0, "moved": 0, "deleted": 0}


def test_external_edit_bumps_revision_and_reindexes(conn):
    n = store.create_note(conn, "1", title="t", body="v1")
    p = store.note_abs_path(conn, n)
    with open(p) as f:
        meta, _ = parse_note_text(f.read())
    with open(p, "w") as f:
        f.write(serialize_note_text(meta, "v2 external"))
    stats = _run(sync.scan_once(conn))
    assert stats["updated"] == 1
    row = conn.execute("SELECT revision FROM notes WHERE id=?",
                       (n["id"],)).fetchone()
    assert row["revision"] == 2
    assert ("index", n["id"]) in conn._test_index_calls


def test_rename_keeps_identity(conn):
    n = store.create_note(conn, "1", title="t", body="b")
    old = store.note_abs_path(conn, n)
    new = os.path.join(os.path.dirname(old), "renamed.md")
    os.rename(old, new)
    stats = _run(sync.scan_once(conn))
    assert stats["moved"] == 1 and stats["deleted"] == 0
    row = conn.execute("SELECT path FROM notes WHERE id=?",
                       (n["id"],)).fetchone()
    assert row["path"] == "1/renamed.md"


def test_disk_delete_soft_deletes_and_deindexes(conn):
    n = store.create_note(conn, "1", title="t", body="b")
    os.remove(store.note_abs_path(conn, n))
    stats = _run(sync.scan_once(conn))
    assert stats["deleted"] == 1
    row = conn.execute("SELECT deleted_at FROM notes WHERE id=?",
                       (n["id"],)).fetchone()
    assert row["deleted_at"] is not None
    assert ("deindex", n["id"]) in conn._test_index_calls


def test_reserved_files_skipped(conn):
    root = store.get_notes_root(conn)
    os.makedirs(f"{root}/1", exist_ok=True)
    for name in ("index.md", "log.md"):
        with open(f"{root}/1/{name}", "w") as f:
            f.write("reserved\n")
    stats = _run(sync.scan_once(conn))
    assert stats["adopted"] == 0
