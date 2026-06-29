import json

import pytest

from db import init_db
import recall_index as ri


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "m.db"))
    yield c
    c.close()


def _save_snapshot(conn, sid, history):
    conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) "
                 "VALUES(?,?,0,0)", (sid, "u1"))
    conn.execute(
        "INSERT INTO messages(id,session_id,role,content,created_at) "
        "VALUES(?,?,?,?,?)",
        (sid + "-m", sid, "history", json.dumps(history), 1))
    conn.commit()


def test_read_history_reads_latest_snapshot(conn):
    hist = [{"role": "user", "content": "I'm a software engineer"},
            {"role": "assistant", "content": "Noted."}]
    _save_snapshot(conn, "s1", hist)
    assert ri._read_history(conn, "s1") == hist


def test_read_history_uses_threaded_conn_not_global(conn, monkeypatch):
    # If _read_history reached for db.get_connection() instead of the threaded
    # conn, this would blow up — force the global to raise to prove it doesn't.
    import db
    _save_snapshot(conn, "s1", [{"role": "user", "content": "hi"}])

    def boom():
        raise AssertionError("must not touch the global connection")
    monkeypatch.setattr(db, "get_connection", boom)
    assert ri._read_history(conn, "s1") == [{"role": "user", "content": "hi"}]


def test_read_history_empty_when_no_row(conn):
    assert ri._read_history(conn, "missing") == []


@pytest.mark.asyncio
async def test_default_upsert_calls_parser_client(monkeypatch):
    seen = {}

    class FakeClient:
        async def agent_memory_upsert(self, user_id, session_id, chunks):
            seen["args"] = (user_id, session_id, chunks)

    monkeypatch.setattr(ri, "_parser_client", FakeClient())
    chunks = [{"chunk_no": 0, "text": "hi", "created_at": 1}]
    await ri._default_upsert("u1", "s1", chunks)
    assert seen["args"] == ("u1", "s1", chunks)
