import pytest
from db import init_db
import memory_store as ms


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "m.db"))
    yield c
    c.close()


def test_supersede_chains_lineage_and_inherits_recall(conn):
    old = ms.add_memory(conn, "u1", "likes lattes", "preference",
                        source="auto", priority=2, now=1000)
    ms.bump_recall(conn, [old], now=1500)   # recall_count -> 1
    new = ms.supersede_memory(conn, old, "u1", "likes americanos",
                              "preference", priority=2, now=2000)
    assert new is not None and new != old
    o = conn.execute("SELECT * FROM memory_entries WHERE id=?", (old,)).fetchone()
    n = conn.execute("SELECT * FROM memory_entries WHERE id=?", (new,)).fetchone()
    assert o["status"] == "superseded"
    assert n["status"] == "active"
    assert n["supersedes"] == old
    assert n["lineage_id"] == o["lineage_id"]   # same family
    assert n["recall_count"] == 1               # inherited
    assert n["source"] == "auto"
    # only the successor remains active
    assert [r["id"] for r in ms.list_active(conn, "u1", now=2000)] == [new]


def test_supersede_returns_none_if_predecessor_not_active(conn):
    old = ms.add_memory(conn, "u1", "x", "fact", now=1000)
    ms.disable_memory(conn, old)
    assert ms.supersede_memory(conn, old, "u1", "y", "fact", now=2000) is None
    # wrong user also yields None
    other = ms.add_memory(conn, "u2", "z", "fact", now=1000)
    assert ms.supersede_memory(conn, other, "u1", "y", "fact", now=2000) is None
