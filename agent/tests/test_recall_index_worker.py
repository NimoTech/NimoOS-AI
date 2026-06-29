import pytest
from db import init_db
import recall_index as ri


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "m.db"))
    yield c
    c.close()


def _sess(conn, sid="s1", user="u1"):
    conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) "
                 "VALUES(?,?,0,0)", (sid, user))
    conn.commit()


def _seed(conn, sid, user, enq):
    ri.maybe_enqueue_index_job(conn, sid, user, now=enq)


def _offset(conn, sid="s1"):
    r = conn.execute("SELECT recall_indexed_msgs, recall_chunk_seq "
                     "FROM sessions WHERE id=?", (sid,)).fetchone()
    return r["recall_indexed_msgs"], r["recall_chunk_seq"]


def _hist2(_sid):
    return [{"role": "user", "content": "I'm a software engineer"},
            {"role": "assistant", "content": "Noted."}]


@pytest.mark.asyncio
async def test_idle_gating_skips_fresh(conn):
    _sess(conn); _seed(conn, "s1", "u1", enq=1000)
    calls = []
    async def up(u, s, c): calls.append((u, s, c))
    res = await ri.process_pending_once(conn, upsert_call=up,
                                        history_loader=_hist2, now=1010)
    assert res is None and calls == []


@pytest.mark.asyncio
async def test_success_indexes_new_and_advances_offset(conn):
    _sess(conn); _seed(conn, "s1", "u1", enq=1000)
    calls = []
    async def up(u, s, c): calls.append((u, s, c))
    res = await ri.process_pending_once(conn, upsert_call=up,
                                        history_loader=_hist2, now=2000)
    assert res == "s1"
    assert calls[0][0] == "u1" and len(calls[0][2]) >= 1
    assert calls[0][2][0]["chunk_no"] == 0
    assert _offset(conn) == (2, len(calls[0][2]))   # msgs + chunk_seq advanced
    assert conn.execute("SELECT COUNT(*) c FROM recall_index_jobs"
                        ).fetchone()["c"] == 0


@pytest.mark.asyncio
async def test_incremental_only_indexes_delta(conn):
    _sess(conn)
    conn.execute("UPDATE sessions SET recall_indexed_msgs=2, recall_chunk_seq=3 "
                 "WHERE id='s1'"); conn.commit()
    _seed(conn, "s1", "u1", enq=1000)
    seen = {}
    def hist3(_s): return _hist2(_s) + [{"role": "user", "content": "and a manager"}]
    async def up(u, s, c): seen["chunks"] = c
    await ri.process_pending_once(conn, upsert_call=up,
                                  history_loader=hist3, now=2000)
    assert len(seen["chunks"]) == 1                 # only the 3rd msg
    assert seen["chunks"][0]["chunk_no"] == 3       # continues from chunk_seq
    assert "manager" in seen["chunks"][0]["text"]
    assert _offset(conn) == (3, 4)


@pytest.mark.asyncio
async def test_no_new_messages_deletes_without_upsert(conn):
    _sess(conn)
    conn.execute("UPDATE sessions SET recall_indexed_msgs=2 WHERE id='s1'")
    conn.commit()
    _seed(conn, "s1", "u1", enq=1000)
    calls = []
    async def up(u, s, c): calls.append(1)
    res = await ri.process_pending_once(conn, upsert_call=up,
                                        history_loader=_hist2, now=2000)
    assert res == "s1" and calls == []
    assert conn.execute("SELECT COUNT(*) c FROM recall_index_jobs"
                        ).fetchone()["c"] == 0


@pytest.mark.asyncio
async def test_failure_keeps_offset_and_retries_then_deletes(conn):
    _sess(conn); _seed(conn, "s1", "u1", enq=1000)
    async def boom(u, s, c): raise RuntimeError("parser down")
    for _ in range(ri.MAX_ATTEMPTS - 1):
        await ri.process_pending_once(conn, upsert_call=boom,
                                      history_loader=_hist2, now=2000)
        assert conn.execute("SELECT status FROM recall_index_jobs WHERE session_id='s1'"
                            ).fetchone()["status"] == "pending"
        assert _offset(conn) == (0, 0)              # NOT advanced on failure
    await ri.process_pending_once(conn, upsert_call=boom,
                                  history_loader=_hist2, now=2000)
    assert conn.execute("SELECT COUNT(*) c FROM recall_index_jobs"
                        ).fetchone()["c"] == 0


@pytest.mark.asyncio
async def test_conditional_delete_preserves_reenqueue(conn):
    # If the user sends a new message mid-processing (re-enqueue → status pending),
    # the success delete (AND status='running') must NOT remove that fresh job.
    _sess(conn); _seed(conn, "s1", "u1", enq=1000)
    async def up(u, s, c):
        # simulate a concurrent run-end re-enqueue while we're "embedding"
        ri.maybe_enqueue_index_job(conn, "s1", "u1", now=2500)
    await ri.process_pending_once(conn, upsert_call=up,
                                  history_loader=_hist2, now=2000)
    row = conn.execute("SELECT status FROM recall_index_jobs WHERE session_id='s1'"
                       ).fetchone()
    assert row is not None and row["status"] == "pending"   # survived
