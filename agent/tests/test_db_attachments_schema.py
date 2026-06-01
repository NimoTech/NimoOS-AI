import sqlite3
import db as db_module


def _init_fresh_db(tmp_path):
    db_path = str(tmp_path / "agent.db")
    db_module.init_db(db_path, snapshots_root=str(tmp_path / "snaps"))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def test_attachments_table_exists(tmp_path):
    conn = _init_fresh_db(tmp_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(attachments)")}
    assert cols == {
        "id", "session_id", "message_id", "filename", "mime",
        "kind", "size_bytes", "rel_path", "meta_json", "created_at"
    }


def test_attachments_indexes_exist(tmp_path):
    conn = _init_fresh_db(tmp_path)
    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='attachments'"
    )}
    assert "idx_attachments_session" in idx
    assert "idx_attachments_msg" in idx


def test_attachments_cascade_on_session_delete(tmp_path):
    conn = _init_fresh_db(tmp_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO sessions (id,user_id,created_at,updated_at) VALUES (?,?,?,?)",
        ("s1", "u1", 0, 0))
    conn.execute(
        "INSERT INTO attachments "
        "(id,session_id,filename,mime,kind,size_bytes,rel_path,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("a1", "s1", "x.txt", "text/plain", "text", 5, "a1__x.txt", 0))
    conn.commit()
    conn.execute("DELETE FROM sessions WHERE id='s1'")
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM attachments WHERE id='a1'").fetchone()[0]
    assert count == 0


def test_init_db_is_idempotent(tmp_path):
    db_path = str(tmp_path / "agent.db")
    snaps = str(tmp_path / "snaps")
    db_module.init_db(db_path, snapshots_root=snaps)
    db_module.init_db(db_path, snapshots_root=snaps)
    conn = sqlite3.connect(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(attachments)")}
    assert "id" in cols


import importlib
import pytest


def test_fresh_db_accepts_document_kind(tmp_path):
    import db as db_module
    importlib.reload(db_module)
    db_path = str(tmp_path / "fresh.db")
    conn = db_module.init_db(db_path)
    conn.execute(
        "INSERT INTO sessions (id,user_id,created_at,updated_at) VALUES (?,?,0,0)",
        ("s1", "u1"))
    conn.execute(
        "INSERT INTO attachments "
        "(id,session_id,message_id,filename,mime,kind,size_bytes,rel_path,"
        " meta_json,created_at) VALUES "
        "('a1','s1',NULL,'x.pdf','application/pdf','document',10,'a1__x.pdf',NULL,0)")
    conn.commit()
    row = conn.execute("SELECT kind FROM attachments WHERE id='a1'").fetchone()
    assert row[0] == "document"


def test_migration_rebuilds_old_check_constraint(tmp_path):
    """Simulate an alpha-stage DB whose attachments CHECK doesn't allow
    'document'. init_db should detect the old schema and rebuild."""
    import db as db_module
    importlib.reload(db_module)
    db_path = str(tmp_path / "old.db")

    # Hand-build the OLD schema (no 'document' in CHECK) and seed a row.
    raw = sqlite3.connect(db_path)
    raw.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE attachments (
            id           TEXT PRIMARY KEY,
            session_id   TEXT NOT NULL,
            message_id   TEXT,
            filename     TEXT NOT NULL,
            mime         TEXT NOT NULL,
            kind         TEXT NOT NULL CHECK(kind IN ('image','text','video','audio','binary')),
            size_bytes   INTEGER NOT NULL,
            rel_path     TEXT NOT NULL,
            meta_json    TEXT,
            created_at   INTEGER NOT NULL,
            FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
        INSERT INTO sessions VALUES ('s1','u1',0,0);
        INSERT INTO attachments VALUES
            ('old1','s1',NULL,'old.txt','text/plain','text',5,'old1__old.txt',NULL,0);
    """)
    raw.commit()
    raw.close()

    # init_db should detect old CHECK and migrate.
    conn = db_module.init_db(db_path)

    # Pre-existing rows preserved.
    row = conn.execute("SELECT id, kind FROM attachments").fetchall()
    assert ("old1", "text") in [(r[0], r[1]) for r in row]

    # And now a document insert is accepted.
    conn.execute(
        "INSERT INTO attachments "
        "(id,session_id,message_id,filename,mime,kind,size_bytes,rel_path,"
        " meta_json,created_at) VALUES "
        "('a1','s1',NULL,'x.pdf','application/pdf','document',10,'a1__x.pdf',NULL,0)")
    conn.commit()
    assert conn.execute(
        "SELECT kind FROM attachments WHERE id='a1'").fetchone()[0] == "document"
