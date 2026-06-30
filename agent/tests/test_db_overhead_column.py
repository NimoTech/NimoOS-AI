from db import init_db


def test_sessions_has_last_overhead_tokens(tmp_path):
    conn = init_db(str(tmp_path / "m.db"))
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert "last_overhead_tokens" in cols
    conn.close()


def test_init_db_reentrant_with_overhead(tmp_path):
    p = str(tmp_path / "m.db")
    init_db(p).close()
    conn = init_db(p)   # must not raise (idempotent ALTER)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert "last_overhead_tokens" in cols
    conn.close()
