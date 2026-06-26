import pytest
from db import init_db
import memory_store as ms


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "m.db"))
    yield c
    c.close()


def test_add_sets_defaults_and_self_lineage(conn):
    mid = ms.add_memory(conn, "u1", "likes oat milk", "preference",
                        source="tool", priority=3, now=1000)
    row = conn.execute("SELECT * FROM memory_entries WHERE id=?", (mid,)).fetchone()
    assert row["lineage_id"] == mid          # self-lineage on ADD
    assert row["status"] == "active"
    assert row["recall_count"] == 0
    assert row["last_recalled_at"] == 1000   # initial = created_at
    assert row["created_at"] == 1000
    assert row["source"] == "tool"
    assert row["priority"] == 3


def test_add_rejects_bad_kind(conn):
    with pytest.raises(ValueError):
        ms.add_memory(conn, "u1", "x", "nonsense")


def test_list_active_excludes_disabled_and_expired(conn):
    a = ms.add_memory(conn, "u1", "active one", "fact", now=1000)
    b = ms.add_memory(conn, "u1", "to disable", "fact", now=1000)
    ms.add_memory(conn, "u1", "expired", "fact", expires_at=500, now=1000)
    ms.add_memory(conn, "u2", "other user", "fact", now=1000)
    ms.disable_memory(conn, b)
    rows = ms.list_active(conn, "u1", now=1000)
    texts = {r["text"] for r in rows}
    assert texts == {"active one"}    # b disabled, expired gone, u2 isolated
    assert a in {r["id"] for r in rows}


def test_find_active_duplicate_is_normalized(conn):
    ms.add_memory(conn, "u1", "Likes  Oat   Milk", "preference", now=1000)
    assert ms.find_active_duplicate(conn, "u1", "likes oat milk") is not None
    assert ms.find_active_duplicate(conn, "u1", "hates oat milk") is None
    assert ms.find_active_duplicate(conn, "u2", "likes oat milk") is None


def test_disable_by_text_matches_substring(conn):
    ms.add_memory(conn, "u1", "lives in Berlin now", "fact", now=1000)
    ms.add_memory(conn, "u1", "works at ACME", "fact", now=1000)
    disabled = ms.disable_by_text(conn, "u1", "berlin")
    assert len(disabled) == 1
    assert len(ms.list_active(conn, "u1", now=1000)) == 1
