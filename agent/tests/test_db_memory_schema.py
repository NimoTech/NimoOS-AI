from db import init_db


def test_memory_entries_table_created(tmp_path):
    conn = init_db(str(tmp_path / "m.db"))
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "memory_entries" in tables
    conn.close()


def test_memory_entries_columns(tmp_path):
    conn = init_db(str(tmp_path / "m.db"))
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(memory_entries)")}
    assert cols == {
        "id", "user_id", "kind", "text", "source", "priority", "status",
        "lineage_id", "supersedes", "recall_count", "last_recalled_at",
        "created_at", "updated_at", "expires_at", "origin_session_id",
    }
    conn.close()


def test_memory_entries_indexes(tmp_path):
    conn = init_db(str(tmp_path / "m.db"))
    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_memory_user_active" in idx
    assert "idx_memory_lineage" in idx
    conn.close()


def test_memory_entries_defaults(tmp_path):
    conn = init_db(str(tmp_path / "m.db"))
    conn.execute(
        "INSERT INTO memory_entries "
        "(id,user_id,kind,text,source,lineage_id,last_recalled_at,"
        " created_at,updated_at) "
        "VALUES ('m1','u1','fact','hi','tool','m1',100,100,100)")
    row = conn.execute("SELECT * FROM memory_entries WHERE id='m1'").fetchone()
    assert row["priority"] == 0
    assert row["status"] == "active"
    assert row["recall_count"] == 0
    assert row["supersedes"] is None
    conn.close()
