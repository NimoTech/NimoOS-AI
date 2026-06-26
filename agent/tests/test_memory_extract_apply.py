import pytest
from db import init_db
import memory_store as ms
import memory_extract as mx


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "m.db"))
    yield c
    c.close()


def _snapshot(conn, user_id):
    return {r["id"]: r["updated_at"] for r in ms.list_active(conn, user_id)}


def test_add_update_noop_and_referenced(conn):
    pref = ms.add_memory(conn, "u1", "likes lattes", "preference", now=1000)
    fact = ms.add_memory(conn, "u1", "lives in Berlin", "fact", now=1000)
    snap = _snapshot(conn, "u1")
    result = {"actions": [
        {"op": "ADD", "id": None, "kind": "fact", "text": "is a software engineer", "priority": 0},
        {"op": "UPDATE", "id": pref, "kind": "preference", "text": "likes americanos", "priority": 0},
        {"op": "NOOP", "id": fact},
    ], "referenced": [fact]}
    counts = mx.apply_extraction(conn, "u1", snap, result, now=2000)
    assert counts == {"added": 1, "updated": 1, "noop": 1, "referenced": 1, "skipped": 0}
    texts = {r["text"] for r in ms.list_active(conn, "u1", now=2000)}
    assert texts == {"is a software engineer", "likes americanos", "lives in Berlin"}
    # referenced bump landed on the fact
    f = conn.execute("SELECT recall_count FROM memory_entries WHERE id=?", (fact,)).fetchone()
    assert f["recall_count"] == 1


def test_optimistic_skip_when_target_changed(conn):
    pref = ms.add_memory(conn, "u1", "likes lattes", "preference", now=1000)
    snap = _snapshot(conn, "u1")
    # someone edits the target after the snapshot
    ms.bump_recall(conn, [pref], now=1500)   # changes updated_at? no — bump sets last_recalled_at only
    conn.execute("UPDATE memory_entries SET updated_at=1600 WHERE id=?", (pref,))
    conn.commit()
    result = {"actions": [{"op": "UPDATE", "id": pref, "kind": "preference",
                           "text": "likes americanos", "priority": 0}], "referenced": []}
    counts = mx.apply_extraction(conn, "u1", snap, result, now=2000)
    assert counts["updated"] == 0 and counts["skipped"] == 1
    # original preference untouched (still active, original text)
    assert {r["text"] for r in ms.list_active(conn, "u1", now=2000)} == {"likes lattes"}


def test_add_dedups_existing(conn):
    ms.add_memory(conn, "u1", "is an engineer", "fact", now=1000)
    snap = _snapshot(conn, "u1")
    result = {"actions": [{"op": "ADD", "id": None, "kind": "fact",
                           "text": "Is An  Engineer", "priority": 0}], "referenced": []}
    counts = mx.apply_extraction(conn, "u1", snap, result, now=2000)
    assert counts["added"] == 0 and counts["skipped"] == 1   # normalized duplicate
    assert len(ms.list_active(conn, "u1", now=2000)) == 1
