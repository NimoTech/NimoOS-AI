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
