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
