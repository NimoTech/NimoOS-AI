import json
import os

import pytest

import db as db_module
from notes import store
from notes.okf import parse_note_text


@pytest.fixture()
def conn(tmp_path):
    c = db_module.init_db(str(tmp_path / "t.db"),
                          snapshots_root=str(tmp_path / "snaps"))
    store.set_notes_root(c, str(tmp_path / "Notes"))
    return c


def test_create_writes_okf_file_and_db_row(conn):
    n = store.create_note(conn, "1", title="My Note", body="hello world",
                          note_type="note", tags=["nas", "hw"],
                          created_by="human")
    assert n["status"] == "curated" and n["revision"] == 1
    abs_path = store.note_abs_path(conn, n)
    assert abs_path.startswith(store.get_notes_root(conn) + "/1/")
    with open(abs_path) as f:
        meta, body = parse_note_text(f.read())
    assert meta["id"] == n["id"] and meta["type"] == "note"
    assert meta["tags"] == ["nas", "hw"] and body.strip() == "hello world"
    row = conn.execute("SELECT * FROM notes WHERE id=?", (n["id"],)).fetchone()
    assert row["user_id"] == "1" and row["title"] == "My Note"


def test_pipeline_notes_default_to_draft(conn):
    n = store.create_note(conn, "1", title="x", body="b",
                          note_type="insight", created_by="pipeline")
    assert n["status"] == "draft"


def test_tags_become_topic_entities_with_mentions(conn):
    n = store.create_note(conn, "1", title="x", body="b", tags=["nas"])
    ent = conn.execute(
        "SELECT * FROM entities WHERE user_id='1' AND type='topic' "
        "AND name='nas'").fetchone()
    assert ent is not None
    men = conn.execute("SELECT * FROM mentions WHERE note_id=?",
                       (n["id"],)).fetchone()
    assert men["entity_id"] == ent["id"]


def test_update_bumps_revision_and_rewrites_file(conn):
    n = store.create_note(conn, "1", title="t", body="v1")
    n2 = store.update_note(conn, "1", n["id"], expected_revision=1,
                           body="v2", status="archived")
    assert n2["revision"] == 2 and n2["status"] == "archived"
    with open(store.note_abs_path(conn, n2)) as f:
        _, body = parse_note_text(f.read())
    assert body.strip() == "v2"


def test_update_conflict_raises(conn):
    n = store.create_note(conn, "1", title="t", body="v1")
    with pytest.raises(store.RevisionConflict) as ei:
        store.update_note(conn, "1", n["id"], expected_revision=99, body="v2")
    assert ei.value.current_revision == 1


def test_cross_user_invisible(conn):
    n = store.create_note(conn, "1", title="t", body="b")
    assert store.get_note(conn, "2", n["id"]) is None
    assert store.list_notes(conn, "2") == []
    with pytest.raises(KeyError):   # update 也按 user 作用域
        store.update_note(conn, "2", n["id"], expected_revision=1, body="x")


def test_soft_delete_removes_file_keeps_row(conn):
    n = store.create_note(conn, "1", title="t", body="b")
    p = store.note_abs_path(conn, n)
    assert store.soft_delete_note(conn, "1", n["id"]) is True
    assert not os.path.exists(p)
    row = conn.execute("SELECT deleted_at FROM notes WHERE id=?",
                       (n["id"],)).fetchone()
    assert row["deleted_at"] is not None
    assert store.list_notes(conn, "1") == []


def test_source_refs_persist(conn):
    refs = [{"path": "/DATA/Documents/a.pdf", "quote": "q"}]
    n = store.create_note(conn, "1", title="t", body="b", source_refs=refs)
    row = conn.execute("SELECT source_refs_json FROM notes WHERE id=?",
                       (n["id"],)).fetchone()
    assert json.loads(row["source_refs_json"]) == refs
