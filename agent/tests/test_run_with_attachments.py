import importlib
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "agent.db"))
    monkeypatch.setenv("NIMOOS_AGENT_DATA_ROOT", str(tmp_path))
    import db as db_module
    importlib.reload(db_module)
    import main as main_module
    importlib.reload(main_module)
    for sid in ("s1", "s2"):
        main_module._db().execute(
            "INSERT INTO sessions (id,user_id,created_at,updated_at) VALUES (?,?,0,0)",
            (sid, "u1"))
    main_module._db().commit()
    return TestClient(main_module.app), main_module


def _upload(client, sid="s1", name="x.txt", body=b"hello"):
    r = client.post(f"/agent/sessions/{sid}/attachments",
                    files={"file": (name, body, "text/plain")},
                    headers={"X-User-Id": "u1"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _run_body(message: str, attachment_ids=None):
    body = {"kind": "user", "message": message}
    if attachment_ids is not None:
        body["attachment_ids"] = attachment_ids
    return body


def _hdr():
    return {
        "X-User-Id": "u1", "X-User-Name": "u1",
        "X-Agent-Provider-Key": "k", "X-Agent-Provider-Url": "http://x",
        "X-Agent-Provider-Type": "openai",
    }


def _mock_sink():
    sink = MagicMock()
    sink.is_done = False
    # _stream_from_sink unpacks subscribe() as `past, sub`; provide a past
    # event list that contains a terminal 'done' so the generator returns
    # immediately without ever awaiting sub.get().
    sink.subscribe.return_value = ([{"type": "done"}], MagicMock())
    return sink


def test_run_with_valid_attachment_backfills_message_id(client):
    c, m = client
    aid = _upload(c)
    with patch.object(m, "_start_run", return_value=_mock_sink()):
        r = c.post("/agent/sessions/s1/run",
                   json=_run_body("hi", [aid]), headers=_hdr())
    assert r.status_code == 200, r.text

    msg_id = m._db().execute(
        "SELECT message_id FROM attachments WHERE id=?", (aid,)
    ).fetchone()[0]
    assert msg_id is not None


def test_run_rejects_unknown_id(client):
    c, m = client
    with patch.object(m, "_start_run", return_value=_mock_sink()):
        r = c.post("/agent/sessions/s1/run",
                   json=_run_body("hi", ["att_bogus"]), headers=_hdr())
    assert r.status_code == 422


def test_run_rejects_cross_session_id(client):
    c, m = client
    aid = _upload(c, sid="s2")
    with patch.object(m, "_start_run", return_value=_mock_sink()):
        r = c.post("/agent/sessions/s1/run",
                   json=_run_body("hi", [aid]), headers=_hdr())
    assert r.status_code == 422


def test_run_rejects_already_bound_id(client):
    c, m = client
    aid = _upload(c)
    m._db().execute(
        "UPDATE attachments SET message_id = 'mZ' WHERE id = ?", (aid,))
    m._db().commit()
    with patch.object(m, "_start_run", return_value=_mock_sink()):
        r = c.post("/agent/sessions/s1/run",
                   json=_run_body("hi", [aid]), headers=_hdr())
    assert r.status_code == 422


def test_run_without_attachments_still_works(client):
    c, m = client
    with patch.object(m, "_start_run", return_value=_mock_sink()):
        r = c.post("/agent/sessions/s1/run",
                   json=_run_body("hi"), headers=_hdr())
    assert r.status_code == 200
