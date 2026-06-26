import pytest
from db import init_db
import memory_store as ms

DAY = 86400


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "m.db"))
    yield c
    c.close()


def _row(conn, mid):
    return conn.execute("SELECT * FROM memory_entries WHERE id=?", (mid,)).fetchone()


def test_recency_decays_monotonically(conn):
    fresh = ms.add_memory(conn, "u1", "fresh", "fact", priority=0, now=1000)
    old = ms.add_memory(conn, "u1", "old", "fact", priority=0, now=1000)
    now = 1000 + 60 * DAY
    # 'old' last recalled long ago vs 'fresh' just now
    conn.execute("UPDATE memory_entries SET last_recalled_at=? WHERE id=?", (now, fresh))
    s_fresh = ms.effective_score(_row(conn, fresh), now)
    s_old = ms.effective_score(_row(conn, old), now)
    assert s_fresh > s_old


def test_recall_count_raises_score(conn):
    a = ms.add_memory(conn, "u1", "a", "fact", priority=1, now=1000)
    b = ms.add_memory(conn, "u1", "b", "fact", priority=1, now=1000)
    conn.execute("UPDATE memory_entries SET recall_count=5 WHERE id=?", (b,))
    now = 1000
    assert ms.effective_score(_row(conn, b), now) > ms.effective_score(_row(conn, a), now)


def test_priority_dominates_rank(conn):
    lo = ms.add_memory(conn, "u1", "low", "fact", priority=0, now=1000)
    hi = ms.add_memory(conn, "u1", "high", "fact", priority=10, now=1000)
    rows = ms.list_active(conn, "u1", now=1000)
    ranked = ms.rank_for_injection(rows, now=1000)
    assert ranked[0]["id"] == hi
    assert ranked[-1]["id"] == lo
