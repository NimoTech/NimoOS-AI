import pytest

import db as db_module
from notes import store
from notes.links import extract_links


@pytest.fixture()
def conn(tmp_path):
    c = db_module.init_db(str(tmp_path / "t.db"),
                          snapshots_root=str(tmp_path / "snaps"))
    store.set_notes_root(c, str(tmp_path / "Notes"))
    return c


def test_extract_links_all_kinds():
    body = ("see [other](/1/other-note.md) and [[Wiki Style]] plus "
            "[doc](/DATA/Documents/a.pdf) and [site](https://example.com) "
            "and [rel](sibling.md)")
    links = extract_links(body)
    kinds = {(l["dst_kind"], l["dst_ref"]) for l in links}
    assert ("note", "/1/other-note.md") in kinds
    assert ("note", "Wiki Style") in kinds
    assert ("file", "/DATA/Documents/a.pdf") in kinds
    assert ("url", "https://example.com") in kinds
    assert ("note", "sibling.md") in kinds


def test_create_persists_links_and_update_replaces(conn):
    a = store.create_note(conn, "1", title="A", body="x")
    b = store.create_note(conn, "1", title="B",
                          body=f"ref [A](/{a['path']})")
    rows = conn.execute("SELECT dst_kind, dst_ref FROM note_links "
                        "WHERE src_note_id=?", (b["id"],)).fetchall()
    assert [(r["dst_kind"], r["dst_ref"]) for r in rows] == \
        [("note", f"/{a['path']}")]
    store.update_note(conn, "1", b["id"], expected_revision=1,
                      body="no links now")
    assert conn.execute("SELECT COUNT(*) FROM note_links "
                        "WHERE src_note_id=?", (b["id"],)).fetchone()[0] == 0


def test_backlinks_resolve_path_and_title(conn):
    a = store.create_note(conn, "1", title="Target Note", body="x")
    store.create_note(conn, "1", title="ByPath", body=f"[t](/{a['path']})")
    store.create_note(conn, "1", title="ByTitle", body="[[Target Note]]")
    backs = store.get_backlinks(conn, "1", a["id"])
    assert {b["title"] for b in backs} == {"ByPath", "ByTitle"}


def test_backlinks_user_scoped(conn):
    a = store.create_note(conn, "1", title="T", body="x")
    store.create_note(conn, "2", title="Other", body="[[T]]")
    assert store.get_backlinks(conn, "1", a["id"]) == []
