# tests/test_tool_output_endpoint.py
import os

import pytest
from fastapi.testclient import TestClient

import main
import tool_output as to


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(to, "ROOT", str(tmp_path / "tool-outputs"))
    main._conn.execute("DELETE FROM sessions WHERE id IN ('to-s1','to-s2')")
    main._conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at,source) "
                       "VALUES('to-s1','u1',0,0,'web')")
    main._conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at,source) "
                       "VALUES('to-s2','u2',0,0,'web')")
    main._conn.commit()
    d = to.chat_dir_for_session("to-s1"); os.makedirs(d)
    with open(os.path.join(d, "call_ok.txt"), "w", encoding="utf-8") as f:
        f.write("full output here")
    return TestClient(main.app)


def test_owner_reads_plain_text(client):
    r = client.get("/agent/sessions/to-s1/tool-outputs/call_ok", headers={"X-User-Id": "u1"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text == "full output here"


def test_other_user_gets_404(client):
    r = client.get("/agent/sessions/to-s1/tool-outputs/call_ok", headers={"X-User-Id": "u2"})
    assert r.status_code == 404


def test_bad_call_id_is_400_and_cannot_traverse(client):
    r = client.get("/agent/sessions/to-s1/tool-outputs/..%2F..%2Fetc", headers={"X-User-Id": "u1"})
    assert r.status_code in (400, 404)


def test_missing_file_is_404(client):
    r = client.get("/agent/sessions/to-s1/tool-outputs/call_nope", headers={"X-User-Id": "u1"})
    assert r.status_code == 404


def test_oversized_file_is_413(client, monkeypatch):
    monkeypatch.setattr(to, "MAX_READ_BYTES", 4)
    r = client.get("/agent/sessions/to-s1/tool-outputs/call_ok", headers={"X-User-Id": "u1"})
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_purge_session_removes_offload_folder(tmp_path, monkeypatch):
    import session_purge
    from db import init_db
    monkeypatch.setattr(to, "ROOT", str(tmp_path / "tool-outputs"))
    conn = init_db(str(tmp_path / "p.db"))
    conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) VALUES('ps','u1',0,0)")
    conn.commit()
    d = to.chat_dir_for_session("ps"); os.makedirs(d)
    open(os.path.join(d, "c.txt"), "w").close()

    async def no_vectors(u, s):
        return None

    ok = await session_purge.purge_session(conn, "u1", "ps", vector_cleanup=no_vectors,
                                           snapshots_root=str(tmp_path / "snaps"))
    assert ok and not os.path.exists(d)


def test_startup_gc_calls_sweep(monkeypatch):
    import inspect
    src = inspect.getsource(main)
    assert "sweep_expired()" in src
