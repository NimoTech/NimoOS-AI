import db as db_module


def _fresh(tmp_path):
    conn = db_module.init_db(str(tmp_path / "t.db"))
    conn.execute(
        "INSERT INTO sessions (id, user_id, created_at, updated_at) VALUES ('s1', '1', 0, 0)"
    )
    conn.commit()
    return conn


def test_column_exists_after_migration(tmp_path):
    conn = _fresh(tmp_path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert "unlocked_tool_categories" in cols


def test_default_empty_when_null(tmp_path):
    conn = _fresh(tmp_path)
    assert db_module.get_unlocked_categories("s1", conn=conn) == []


def test_round_trip(tmp_path):
    conn = _fresh(tmp_path)
    db_module.set_unlocked_categories("s1", ["apps", "files"], conn=conn)
    assert sorted(db_module.get_unlocked_categories("s1", conn=conn)) == ["apps", "files"]


def test_migration_idempotent(tmp_path):
    p = str(tmp_path / "t.db")
    db_module.init_db(p)
    conn2 = db_module.init_db(p)            # 二次不应报错
    cols = {r["name"] for r in conn2.execute("PRAGMA table_info(sessions)")}
    assert "unlocked_tool_categories" in cols
