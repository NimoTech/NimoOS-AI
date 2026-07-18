import inspect

import pytest
from db import init_db
import memory_extract as mx


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "m.db"))
    yield c
    c.close()


def _seed_running(conn, sid="s1", user="u1"):
    mx.maybe_enqueue_extract_job(conn, sid, user, provider_url="u",
        provider_key="k", provider_type="t", model_name="m", now=1000)
    conn.execute("UPDATE memory_extract_jobs SET status='running' WHERE session_id=?",
                 (sid,))
    conn.commit()


def test_requeue_orphaned_flips_running_to_pending(conn):
    _seed_running(conn)
    n = mx._requeue_orphaned(conn)
    assert n == 1
    row = conn.execute("SELECT status FROM memory_extract_jobs WHERE session_id='s1'"
                       ).fetchone()
    assert row["status"] == "pending"


def test_start_worker_references_requeue_orphaned():
    assert "_requeue_orphaned" in inspect.getsource(mx.start_worker)
