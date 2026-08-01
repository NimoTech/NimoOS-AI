import os

import pytest

import db as db_module
from notes import reserved, store


@pytest.fixture()
def conn(tmp_path):
    c = db_module.init_db(str(tmp_path / "t.db"),
                          snapshots_root=str(tmp_path / "snaps"))
    store.set_notes_root(c, str(tmp_path / "Notes"))
    return c


def test_render_creates_okf_reserved_files(conn):
    store.create_note(conn, "1", title="Alpha", body="a",
                      note_type="note", description="first")
    store.create_note(conn, "1", title="Beta", body="b", note_type="insight",
                      created_by="pipeline")
    reserved.render_for_user(conn, "1")
    root = store.get_notes_root(conn)
    idx = open(f"{root}/1/index.md").read()
    assert 'okf_version: "0.1"' in idx
    assert "[Alpha](" in idx and "- first" in idx
    assert "## Insight" in idx                       # grouped by type
    log = open(f"{root}/1/log.md").read()
    assert "**Creation**" in log and "Alpha" in log


def test_log_is_bounded(conn, monkeypatch):
    monkeypatch.setattr(reserved, "LOG_CAP", 3)
    for i in range(5):
        store.create_note(conn, "1", title=f"n{i}", body="b")
    reserved.render_for_user(conn, "1")
    log = open(f"{store.get_notes_root(conn)}/1/log.md").read()
    assert log.count("**Creation**") == 3


def test_soft_deleted_appear_as_deprecation(conn):
    n = store.create_note(conn, "1", title="Gone", body="b")
    store.soft_delete_note(conn, "1", n["id"])
    reserved.render_for_user(conn, "1")
    log = open(f"{store.get_notes_root(conn)}/1/log.md").read()
    assert "**Deprecation**" in log and "Gone" in log
    idx = open(f"{store.get_notes_root(conn)}/1/index.md").read()
    assert "Gone" not in idx                          # soft-deleted notes excluded from index
