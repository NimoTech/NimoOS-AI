import db as db_module


def test_new_db_sessions_default_agent_type_general(tmp_path):
    conn = db_module.init_db(str(tmp_path / "a.db"),
                             snapshots_root=str(tmp_path / "snap"))
    conn.execute(
        "INSERT INTO sessions (id, user_id, created_at, updated_at) "
        "VALUES ('s1', 'u1', 0, 0)")
    conn.commit()
    row = conn.execute(
        "SELECT agent_type FROM sessions WHERE id='s1'").fetchone()
    assert row["agent_type"] == "general"


def test_agent_type_migration_idempotent(tmp_path):
    # Second init_db on the same file must not raise and must not duplicate
    # the column (exercises the idempotent ALTER path).
    p = str(tmp_path / "a.db")
    conn = db_module.init_db(p, snapshots_root=str(tmp_path / "snap"))
    conn.close()
    conn = db_module.init_db(p, snapshots_root=str(tmp_path / "snap"))
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(sessions)")]
    assert cols.count("agent_type") == 1


def test_agent_type_migration_fills_existing_rows(tmp_path):
    """Rows inserted before the migration get the default 'general' value."""
    import sqlite3
    db_path = str(tmp_path / "a.db")
    # Simulate a pre-migration DB: sessions table without agent_type
    raw = sqlite3.connect(db_path)
    raw.execute(
        "CREATE TABLE sessions "
        "(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
        "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
    )
    raw.execute("INSERT INTO sessions VALUES ('s1','u1',0,0)")
    raw.commit()
    raw.close()

    conn = db_module.init_db(db_path, snapshots_root=str(tmp_path / "snap"))
    row = conn.execute("SELECT agent_type FROM sessions WHERE id='s1'").fetchone()
    assert row["agent_type"] == "general"
