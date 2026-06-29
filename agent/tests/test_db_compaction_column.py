from db import init_db


def test_sessions_has_rolling_summary_column(tmp_path):
    conn = init_db(str(tmp_path / "m.db"))
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert "rolling_summary" in cols
    conn.close()


def test_init_db_is_reentrant(tmp_path):
    p = str(tmp_path / "m.db")
    init_db(p).close()
    # second init on the same file must not raise (idempotent ALTER)
    conn = init_db(p)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert "rolling_summary" in cols
    conn.close()
