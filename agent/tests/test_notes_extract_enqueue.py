import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from db import init_db
from notes import store as notes_store


def _conn(tmp_path):
    return init_db(str(tmp_path / "m.db"))


def test_auto_extract_defaults_on(tmp_path):
    conn = _conn(tmp_path)
    assert notes_store.is_auto_extract_enabled(conn, "1") is True


def test_auto_extract_toggle_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    notes_store.set_auto_extract(conn, "1", False)
    assert notes_store.is_auto_extract_enabled(conn, "1") is False
    assert notes_store.is_auto_extract_enabled(conn, "2") is True  # per-user
    notes_store.set_auto_extract(conn, "1", True)
    assert notes_store.is_auto_extract_enabled(conn, "1") is True


def test_notes_extract_jobs_table_exists(tmp_path):
    conn = _conn(tmp_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(notes_extract_jobs)")}
    assert {"session_id", "user_id", "status", "attempts", "provider_url",
            "provider_key", "provider_type", "model_name", "last_error",
            "enqueued_at", "updated_at"} <= cols


def test_notes_extraction_columns_backfilled(tmp_path):
    # simulate a pre-M3 DB missing the three extraction columns
    import sqlite3
    p = str(tmp_path / "old.db")
    raw = sqlite3.connect(p)
    raw.execute("""CREATE TABLE notes (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, path TEXT NOT NULL,
        title TEXT, description TEXT, type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft', content_hash TEXT NOT NULL,
        source_refs_json TEXT, created_by TEXT NOT NULL DEFAULT 'human',
        revision INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER, updated_at INTEGER, deleted_at INTEGER)""")
    raw.commit(); raw.close()
    conn = init_db(p)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(notes)")}
    assert {"extraction_status", "extracted_at", "content_hash_at_extraction"} <= cols


def _mk_session(conn, sid, uid, source="web"):
    conn.execute(
        "INSERT INTO sessions (id, user_id, source, created_at, updated_at) "
        "VALUES (?,?,?,0,0)", (sid, uid, source))
    conn.commit()


ENQ = dict(provider_url="http://x", provider_key="k",
           provider_type="openai", model_name="m")


def test_enqueue_web_session(tmp_path):
    import notes_extract
    conn = _conn(tmp_path)
    _mk_session(conn, "s1", "1")
    assert notes_extract.maybe_enqueue_notes_job(conn, "s1", "1", **ENQ) is True
    row = conn.execute("SELECT * FROM notes_extract_jobs").fetchone()
    assert row["session_id"] == "s1" and row["status"] == "pending"


def test_enqueue_skips_when_disabled(tmp_path):
    import notes_extract
    conn = _conn(tmp_path)
    _mk_session(conn, "s1", "1")
    notes_store.set_auto_extract(conn, "1", False)
    assert notes_extract.maybe_enqueue_notes_job(conn, "s1", "1", **ENQ) is False
    assert conn.execute("SELECT COUNT(*) c FROM notes_extract_jobs").fetchone()["c"] == 0


def test_enqueue_skips_channel_sessions(tmp_path):
    import notes_extract
    conn = _conn(tmp_path)
    _mk_session(conn, "s1", "1", source="telegram")
    assert notes_extract.maybe_enqueue_notes_job(conn, "s1", "1", **ENQ) is False


def test_enqueue_coalesces(tmp_path):
    import notes_extract
    conn = _conn(tmp_path)
    _mk_session(conn, "s1", "1")
    notes_extract.maybe_enqueue_notes_job(conn, "s1", "1", now=100, **ENQ)
    conn.execute("UPDATE notes_extract_jobs SET status='running', attempts=2")
    conn.commit()
    notes_extract.maybe_enqueue_notes_job(conn, "s1", "1", now=200, **ENQ)
    row = conn.execute("SELECT * FROM notes_extract_jobs").fetchone()
    assert (row["status"], row["attempts"], row["enqueued_at"]) == ("pending", 0, 200)
