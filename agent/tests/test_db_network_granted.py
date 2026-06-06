import sqlite3
import db as dbmod


def _fresh(tmp_path):
    p = str(tmp_path / "agent.db")
    conn = dbmod.init_db(p, snapshots_root=str(tmp_path / "snap"))
    return conn


def test_new_db_has_network_granted_column(tmp_path):
    conn = _fresh(tmp_path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert "network_granted" in cols


def test_grant_and_query(tmp_path):
    conn = _fresh(tmp_path)
    conn.execute(
        "INSERT INTO sessions (id,user_id,title,created_at,updated_at) "
        "VALUES ('s1','u1','t',0,0)")
    conn.commit()
    assert dbmod.is_network_granted(conn, "s1") is False
    dbmod.grant_network(conn, "s1")
    assert dbmod.is_network_granted(conn, "s1") is True


def test_migration_on_legacy_db(tmp_path):
    p = str(tmp_path / "agent.db")
    raw = sqlite3.connect(p)
    raw.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
        "title TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)")
    raw.execute("INSERT INTO sessions VALUES ('old','u',NULL,0,0)")
    raw.commit(); raw.close()
    conn = dbmod.init_db(p, snapshots_root=str(tmp_path / "snap"))
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert "network_granted" in cols
    assert dbmod.is_network_granted(conn, "old") is False


def test_network_granted_migration_idempotent(tmp_path):
    # Second init_db on the same file must not raise and must not duplicate
    # the column (exercises the idempotent ALTER path).
    p = str(tmp_path / "agent.db")
    conn = dbmod.init_db(p, snapshots_root=str(tmp_path / "snap"))
    conn.close()
    conn = dbmod.init_db(p, snapshots_root=str(tmp_path / "snap"))
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(sessions)")]
    assert cols.count("network_granted") == 1
