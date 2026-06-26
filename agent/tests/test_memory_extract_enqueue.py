import pytest
from db import init_db
import memory_extract as mx


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "m.db"))
    yield c
    c.close()


def _job(conn, sid):
    return conn.execute(
        "SELECT * FROM memory_extract_jobs WHERE session_id=?", (sid,)).fetchone()


def test_enqueue_when_enabled(conn):
    ok = mx.maybe_enqueue_extract_job(
        conn, "s1", "u1", provider_url="http://x", provider_key="k",
        provider_type="openai", model_name="m", now=1000)
    assert ok is True
    r = _job(conn, "s1")
    assert r["status"] == "pending" and r["user_id"] == "u1"
    assert r["provider_key"] == "k" and r["model_name"] == "m"
    assert r["enqueued_at"] == 1000


def test_coalesces_same_session(conn):
    mx.maybe_enqueue_extract_job(conn, "s1", "u1", provider_url="a",
        provider_key="k1", provider_type="t", model_name="m1", now=1000)
    mx.maybe_enqueue_extract_job(conn, "s1", "u1", provider_url="b",
        provider_key="k2", provider_type="t", model_name="m2", now=2000)
    rows = conn.execute(
        "SELECT * FROM memory_extract_jobs WHERE session_id='s1'").fetchall()
    assert len(rows) == 1                      # one row, not two
    assert rows[0]["enqueued_at"] == 2000      # refreshed
    assert rows[0]["model_name"] == "m2"       # creds refreshed
    assert rows[0]["status"] == "pending"


def test_not_enqueued_when_memory_disabled(conn):
    conn.execute("INSERT INTO user_settings(user_id,key,value,updated_at) "
                 "VALUES('u1','memory_enabled','0',0)")
    ok = mx.maybe_enqueue_extract_job(
        conn, "s1", "u1", provider_url="x", provider_key="k",
        provider_type="t", model_name="m", now=1000)
    assert ok is False
    assert _job(conn, "s1") is None
