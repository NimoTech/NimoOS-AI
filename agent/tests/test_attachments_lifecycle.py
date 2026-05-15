import importlib
import os
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "agent.db"))
    monkeypatch.setenv("NIMOOS_AGENT_DATA_ROOT", str(tmp_path))
    import db as db_module
    importlib.reload(db_module)
    import main as main_module
    importlib.reload(main_module)
    main_module._db().execute(
        "INSERT INTO sessions (id,user_id,created_at,updated_at) VALUES (?,?,0,0)",
        ("sess1", "u1"))
    main_module._db().commit()
    return TestClient(main_module.app), main_module


def _upload(client, name="x.txt", body=b"hello"):
    r = client.post("/agent/sessions/sess1/attachments",
                    files={"file": (name, body, "text/plain")},
                    headers={"X-User-Id": "u1"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_list_includes_uploaded(client):
    c, _ = client
    aid = _upload(c)
    r = c.get("/agent/sessions/sess1/attachments",
              headers={"X-User-Id": "u1"})
    assert r.status_code == 200
    ids = [a["id"] for a in r.json()]
    assert aid in ids


def test_delete_draft_attachment(client):
    c, _ = client
    aid = _upload(c)
    r = c.delete(f"/agent/sessions/sess1/attachments/{aid}",
                 headers={"X-User-Id": "u1"})
    assert r.status_code == 200
    listed = c.get("/agent/sessions/sess1/attachments",
                   headers={"X-User-Id": "u1"}).json()
    assert all(a["id"] != aid for a in listed)


def test_delete_bound_attachment_returns_409(client):
    c, m = client
    aid = _upload(c)
    m._db().execute(
        "UPDATE attachments SET message_id = ? WHERE id = ?", ("msg1", aid))
    m._db().commit()
    r = c.delete(f"/agent/sessions/sess1/attachments/{aid}",
                 headers={"X-User-Id": "u1"})
    assert r.status_code == 409


def test_raw_streams_bytes(client):
    c, _ = client
    aid = _upload(c, name="t.txt", body=b"hello world")
    r = c.get(f"/agent/sessions/sess1/attachments/{aid}/raw",
              headers={"X-User-Id": "u1"})
    assert r.status_code == 200
    assert r.content == b"hello world"


def test_raw_requires_session_ownership(client):
    c, _ = client
    aid = _upload(c)
    r = c.get(f"/agent/sessions/sess1/attachments/{aid}/raw",
              headers={"X-User-Id": "different"})
    # _assert_owns_session returns 404 for cross-user; accept both for portability
    assert r.status_code in (403, 404)


def test_delete_nonexistent_returns_404(client):
    c, _ = client
    r = c.delete("/agent/sessions/sess1/attachments/att_nope",
                 headers={"X-User-Id": "u1"})
    assert r.status_code == 404
