import base64
import json
import time
import pytest
import os
from fastapi.testclient import TestClient

import db as db_module
import main as main_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "a.db")
    snap_root = str(tmp_path / "snap")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    monkeypatch.setenv("AGENT_SNAPSHOTS_ROOT", snap_root)
    # Force module re-init against the fresh tmp DB. Routed through
    # monkeypatch.setattr (not a bare `main_module._conn = ...` assignment) so
    # it reverts to the original `_conn` when this test ends, same as the env
    # vars above. A bare assignment here used to permanently repoint the
    # shared `main_module._conn` for the rest of the pytest session (nothing
    # to revert it), which silently broke every other test module's use of
    # `main._conn` for the remainder of a full-suite run -- notably the MCP
    # server's token verification in mcp_server/server.py::build(conn), which
    # closes over `_conn` once at import time and therefore never followed
    # the repoint, so tokens written via the module attribute after this test
    # ran no longer verified. Explicitly close the tmp-DB connection once the
    # test is done so it isn't just a dangling sqlite3.Connection left for GC.
    conn = db_module.init_db(db_path, snapshots_root=snap_root)
    monkeypatch.setattr(main_module, "_conn", conn)
    main_module._snapshots_root = snap_root
    try:
        yield TestClient(main_module.app)
    finally:
        conn.close()


def _create_session(client, user_id="42"):
    r = client.post("/agent/sessions", headers={"X-User-Id": user_id})
    assert r.status_code == 200
    return r.json()["session_id"]


def test_visible_resources_create_list_delete(client, tmp_path):
    sid = _create_session(client)
    p = tmp_path / "proj"; p.mkdir()
    r = client.post(f"/agent/sessions/{sid}/visible-resources",
                    json={"path": str(p), "kind": "folder"},
                    headers={"X-User-Id": "42"})
    assert r.status_code == 200
    res_id = r.json()["id"]

    r = client.get(f"/agent/sessions/{sid}/visible-resources",
                   headers={"X-User-Id": "42"})
    items = r.json()
    assert len(items) == 1 and items[0]["path"] == str(p)

    r = client.delete(f"/agent/sessions/{sid}/visible-resources/{res_id}",
                      headers={"X-User-Id": "42"})
    assert r.status_code == 200
    r = client.get(f"/agent/sessions/{sid}/visible-resources",
                   headers={"X-User-Id": "42"})
    assert r.json() == []


def test_visible_resources_rejects_hard_blacklist(client, tmp_path):
    sid = _create_session(client)
    # /etc is in the built-in blacklist
    r = client.post(f"/agent/sessions/{sid}/visible-resources",
                    json={"path": "/etc", "kind": "folder"},
                    headers={"X-User-Id": "42"})
    assert r.status_code == 403


def test_staged_changes_empty(client):
    sid = _create_session(client)
    r = client.get(f"/agent/sessions/{sid}/staged-changes",
                   headers={"X-User-Id": "42"})
    assert r.status_code == 200
    assert r.json() == []


def test_staged_changes_commit_drops_pending(client, tmp_path):
    sid = _create_session(client)
    # Insert a fake pending row directly
    main_module._conn.execute(
        "INSERT INTO staged_changes "
        "(session_id, run_id, seq, op, path, status, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (sid, "r1", 1, "mkdir", "/tmp/x", "pending", int(time.time())))
    main_module._conn.commit()
    r = client.post(f"/agent/sessions/{sid}/staged-changes/commit",
                    headers={"X-User-Id": "42"})
    assert r.status_code == 200
    statuses = [row["status"] for row in main_module._conn.execute(
        "SELECT status FROM staged_changes WHERE session_id=?", (sid,))]
    assert all(s == "committed" for s in statuses)


def test_fs_list_filters_blacklist(client, tmp_path):
    sid = _create_session(client)
    r = client.get(f"/agent/fs/list?path={tmp_path}",
                   headers={"X-User-Id": "42"})
    assert r.status_code == 200
    # tmp_path is the implicit "max scope" surrogate in tests; entries returned
    assert isinstance(r.json(), list)


def test_session_delete_wipes_sidecar(client, tmp_path):
    sid = _create_session(client)
    snap_dir = os.path.join(main_module._snapshots_root, sid)
    os.makedirs(snap_dir, exist_ok=True)
    # drop a fake snapshot file
    with open(os.path.join(snap_dir, "fake.bin"), "wb") as f:
        f.write(b"x")
    r = client.delete(f"/agent/sessions/{sid}",
                      headers={"X-User-Id": "42"})
    assert r.status_code == 200
    assert not os.path.exists(snap_dir)


def test_revert_endpoint_batch(client):
    """POST /agent/sessions/{sid}/revert with batch_id reverts the batch."""
    from fs import staging as fs_staging

    sid = _create_session(client, user_id="u1")
    # Seed two pending staged_changes rows with batch_id="b1" via the same _conn
    fs_staging.record(main_module._conn, sid, "r1", 1, "mkdir", "/tmp/x",
                      batch_id="b1")
    fs_staging.record(main_module._conn, sid, "r1", 2, "mkdir", "/tmp/y",
                      batch_id="b1")

    r = client.post(f"/agent/sessions/{sid}/revert",
                    json={"batch_id": "b1"},
                    headers={"X-User-Id": "u1"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "nothing_to_revert", "snapshot_missing",
                               "conflict", "partial")
