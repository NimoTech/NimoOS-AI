# NimoOS-AI/agent/tests/test_channels_schema.py
import db as db_module


def _tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r["name"] for r in rows}


def test_channel_tables_exist(tmp_path):
    conn = db_module.init_db(str(tmp_path / "t.db"),
                             snapshots_root=str(tmp_path / "snaps"))
    assert {"channel_instances", "channel_bindings",
            "channel_pairing_codes", "channel_chats"} <= _tables(conn)


def test_sessions_source_column_with_default(tmp_path):
    conn = db_module.init_db(str(tmp_path / "t.db"),
                             snapshots_root=str(tmp_path / "snaps"))
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert "source" in cols
    conn.execute(
        "INSERT INTO sessions (id, user_id, created_at, updated_at) "
        "VALUES ('s1','u1',1,1)")
    row = conn.execute("SELECT source FROM sessions WHERE id='s1'").fetchone()
    assert row["source"] == "web"


def test_binding_unique_per_instance_and_external_user(tmp_path):
    import sqlite3
    import pytest
    conn = db_module.init_db(str(tmp_path / "t.db"),
                             snapshots_root=str(tmp_path / "snaps"))
    conn.execute(
        "INSERT INTO channel_bindings (id, instance_id, external_user_id, user_id, created_at) "
        "VALUES ('b1','i1','tg1','u1',1)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO channel_bindings (id, instance_id, external_user_id, user_id, created_at) "
            "VALUES ('b2','i1','tg1','u2',1)")
