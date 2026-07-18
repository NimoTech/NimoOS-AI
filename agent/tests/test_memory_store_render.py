import pytest
from db import init_db
import memory_store as ms


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "m.db"))
    yield c
    c.close()


def test_empty_block_when_no_memories(conn):
    assert ms.render_user_block(conn, "u1", now=1000) == ""


def test_block_lists_entries_with_kind(conn):
    ms.add_memory(conn, "u1", "likes oat milk", "preference", priority=5, now=1000)
    ms.add_memory(conn, "u1", "learning Spanish", "goal", priority=1, now=1000)
    block = ms.render_user_block(conn, "u1", now=1000)
    assert block.startswith("## About this user")
    assert "- (preference) likes oat milk" in block
    assert "- (goal) learning Spanish" in block
    # higher priority appears first
    assert block.index("oat milk") < block.index("Spanish")


def test_entry_budget_truncates(conn):
    for i in range(40):
        ms.add_memory(conn, "u1", f"fact {i}", "fact", priority=i, now=1000)
    block = ms.render_user_block(conn, "u1", now=1000, max_entries=5)
    assert block.count("- (fact) ") == 5


def test_char_budget_truncates(conn):
    ms.add_memory(conn, "u1", "x" * 50, "fact", priority=2, now=1000)
    ms.add_memory(conn, "u1", "y" * 50, "fact", priority=1, now=1000)
    block = ms.render_user_block(conn, "u1", now=1000, max_chars=60)
    assert "x" * 50 in block      # first fits
    assert "y" * 50 not in block  # second exceeds budget


def test_bump_recall_increments_and_stamps(conn):
    mid = ms.add_memory(conn, "u1", "z", "fact", now=1000)
    ms.bump_recall(conn, [mid], now=2000)
    row = conn.execute("SELECT * FROM memory_entries WHERE id=?", (mid,)).fetchone()
    assert row["recall_count"] == 1
    assert row["last_recalled_at"] == 2000
