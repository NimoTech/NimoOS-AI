import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from db import init_db
from notes import store as notes_store


def _conn(tmp_path):
    conn = init_db(str(tmp_path / "m.db"))
    notes_store.set_notes_root(conn, str(tmp_path / "Notes"))
    return conn


def test_distill_jobs_table_exists(tmp_path):
    conn = _conn(tmp_path)
    cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(notes_distill_jobs)")}
    assert cols == {"file_path", "user_id", "root_id", "file_mtime",
                    "status", "attempts", "origin", "last_error",
                    "enqueued_at", "updated_at"}


def test_distill_roots_default_empty_and_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    assert notes_store.get_distill_roots(conn, "u1") == []
    notes_store.set_distill_roots(conn, "u1", ["r1", "r2"])
    assert notes_store.get_distill_roots(conn, "u1") == ["r1", "r2"]


def test_distill_roots_survives_corrupt_value(tmp_path):
    conn = _conn(tmp_path)
    notes_store._set_setting(conn, "u1", notes_store.DISTILL_ROOTS_KEY, "{oops")
    assert notes_store.get_distill_roots(conn, "u1") == []


def test_daily_cap_default_and_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    assert notes_store.get_daily_cap(conn, "u1") == 50
    notes_store.set_daily_cap(conn, "u1", 7)
    assert notes_store.get_daily_cap(conn, "u1") == 7


def test_daily_cap_rejects_garbage_falls_back_to_default(tmp_path):
    conn = _conn(tmp_path)
    notes_store._set_setting(conn, "u1", notes_store.DISTILL_CAP_KEY, "abc")
    assert notes_store.get_daily_cap(conn, "u1") == 50


def test_background_model_default_empty(tmp_path):
    conn = _conn(tmp_path)
    assert notes_store.get_background_model(conn, "u1") == ""
    notes_store.set_background_model(conn, "u1", "cloud:3:gpt-4o-mini")
    assert notes_store.get_background_model(conn, "u1") == "cloud:3:gpt-4o-mini"


def test_quota_consumes_and_resets_next_day(tmp_path):
    conn = _conn(tmp_path)
    notes_store.set_daily_cap(conn, "u1", 2)
    assert notes_store.quota_remaining(conn, "u1", day="20260727") == 2
    notes_store.quota_consume(conn, "u1", day="20260727")
    notes_store.quota_consume(conn, "u1", day="20260727")
    assert notes_store.quota_remaining(conn, "u1", day="20260727") == 0
    # new day resets
    assert notes_store.quota_remaining(conn, "u1", day="20260728") == 2


def test_update_note_can_refresh_source_refs(tmp_path):
    conn = _conn(tmp_path)
    note = notes_store.create_note(
        conn, "u1", title="T", body="b", note_type="summary",
        source_refs=[{"path": "/DATA/a.pdf", "mtime": 1}],
        created_by="pipeline")
    updated = notes_store.update_note(
        conn, "u1", note["id"], expected_revision=1, body="b2",
        source_refs=[{"path": "/DATA/a.pdf", "mtime": 2}])
    assert updated["source_refs"] == [{"path": "/DATA/a.pdf", "mtime": 2}]
    row = conn.execute("SELECT source_refs_json FROM notes WHERE id=?",
                       (note["id"],)).fetchone()
    assert '"mtime": 2' in row["source_refs_json"]
