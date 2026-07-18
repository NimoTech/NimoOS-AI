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


def test_requeue_orphaned_flips_running_to_pending(conn):
    _sess(conn)
    ri.maybe_enqueue_index_job(conn, "s1", "u1", now=1000)
    conn.execute("UPDATE recall_index_jobs SET status='running' WHERE session_id='s1'")
    conn.commit()
    n = ri._requeue_orphaned(conn)
    assert n == 1
    row = conn.execute("SELECT status FROM recall_index_jobs WHERE session_id='s1'"
                       ).fetchone()
    assert row["status"] == "pending"
