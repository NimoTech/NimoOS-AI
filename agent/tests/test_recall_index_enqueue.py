import pytest
from db import init_db
import recall_index as ri
from recall_index import maybe_enqueue_index_job, IDLE_SECONDS


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "m.db"))
    yield c
    c.close()


def _conn(tmp_path):
    conn = init_db(str(tmp_path / "m.db"))
    # memory_enabled defaults on; insert explicitly so the gate never flakes
    conn.execute("INSERT INTO user_settings(user_id,key,value,updated_at) "
                 "VALUES('u1','memory_enabled','1',0)")
    conn.commit()
    return conn


def test_sessions_has_recall_offset_columns(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert {"recall_indexed_msgs", "recall_chunk_seq"} <= cols


def test_enqueue_when_enabled_and_coalesces(conn):
    assert ri.maybe_enqueue_index_job(conn, "s1", "u1", now=1000) is True
    assert ri.maybe_enqueue_index_job(conn, "s1", "u1", now=2000) is True
    rows = conn.execute(
        "SELECT * FROM recall_index_jobs WHERE session_id='s1'").fetchall()
    assert len(rows) == 1                      # coalesced
    assert rows[0]["enqueued_at"] == 1000      # kept at earliest pending enqueue
    assert rows[0]["status"] == "pending"


def test_not_enqueued_when_disabled(conn):
    conn.execute("INSERT INTO user_settings(user_id,key,value,updated_at) "
                 "VALUES('u1','memory_enabled','0',0)")
    assert ri.maybe_enqueue_index_job(conn, "s1", "u1", now=1000) is False
    assert conn.execute("SELECT COUNT(*) c FROM recall_index_jobs"
                        ).fetchone()["c"] == 0


def test_chunk_messages_starts_at_offset_and_splits():
    msgs = [
        {"role": "user", "content": "a" * 1500},
        {"role": "assistant", "content": "b" * 1500},
        {"role": "user", "content": "c" * 100},
    ]
    chunks = ri.chunk_messages(msgs, start_chunk_no=7, now=42, max_chars=2000)
    assert [c["chunk_no"] for c in chunks] == [7, 8]   # numbering continues from 7
    assert all(c["created_at"] == 42 for c in chunks)
    assert "user: " + "a" * 1500 in chunks[0]["text"]


def test_chunk_messages_skips_empty():
    msgs = [{"role": "user", "content": ""},
            {"role": "assistant", "content": "hi"}]
    chunks = ri.chunk_messages(msgs, start_chunk_no=0, now=1, max_chars=2000)
    assert len(chunks) == 1 and chunks[0]["chunk_no"] == 0


def test_reenqueue_does_not_postpone_claimability(tmp_path):
    # An active session re-enqueues on every run end. The job must become
    # claimable IDLE_SECONDS after the FIRST enqueue, not be postponed forever.
    conn = _conn(tmp_path)
    assert maybe_enqueue_index_job(conn, "s1", "u1", now=1000)
    assert maybe_enqueue_index_job(conn, "s1", "u1", now=1100)
    row = conn.execute("SELECT enqueued_at FROM recall_index_jobs "
                       "WHERE session_id='s1'").fetchone()
    assert row["enqueued_at"] == 1000


def test_immediate_enqueue_is_claimable_now(tmp_path):
    conn = _conn(tmp_path)
    assert maybe_enqueue_index_job(conn, "s1", "u1", now=1000, immediate=True)
    row = conn.execute("SELECT enqueued_at FROM recall_index_jobs "
                       "WHERE session_id='s1'").fetchone()
    assert row["enqueued_at"] == 1000 - IDLE_SECONDS


def test_normal_reenqueue_keeps_immediate_backdate(tmp_path):
    # A later normal enqueue must not push an immediate job back out.
    conn = _conn(tmp_path)
    maybe_enqueue_index_job(conn, "s1", "u1", now=1000, immediate=True)
    maybe_enqueue_index_job(conn, "s1", "u1", now=1010)
    row = conn.execute("SELECT enqueued_at FROM recall_index_jobs "
                       "WHERE session_id='s1'").fetchone()
    assert row["enqueued_at"] == 1000 - IDLE_SECONDS


def test_disabled_memory_short_circuits_immediate(tmp_path):
    conn = init_db(str(tmp_path / "m2.db"))
    conn.execute("INSERT INTO user_settings(user_id,key,value,updated_at) "
                 "VALUES('u1','memory_enabled','0',0)")
    conn.commit()
    assert not maybe_enqueue_index_job(conn, "s1", "u1", now=1000, immediate=True)
