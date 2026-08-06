# NimoOS-AI/agent/tests/test_main_max_turns.py
from fastapi.testclient import TestClient
import main


def _client():
    return TestClient(main.app)


def test_get_default_10():
    c = _client()
    r = c.get("/agent/user-settings/max-turns", headers={"X-User-Id": "mtu1"})
    assert r.status_code == 200
    assert r.json() == {"max_turns": 10}


def test_put_then_get_roundtrip_unlimited():
    c = _client()
    assert c.put("/agent/user-settings/max-turns", json={"max_turns": 0},
                 headers={"X-User-Id": "mtu2"}).status_code == 200
    r = c.get("/agent/user-settings/max-turns", headers={"X-User-Id": "mtu2"})
    assert r.json() == {"max_turns": 0}


def test_put_finite_value():
    c = _client()
    c.put("/agent/user-settings/max-turns", json={"max_turns": 50},
          headers={"X-User-Id": "mtu3"})
    assert c.get("/agent/user-settings/max-turns",
                 headers={"X-User-Id": "mtu3"}).json() == {"max_turns": 50}


def test_put_negative_rejected():
    c = _client()
    r = c.put("/agent/user-settings/max-turns", json={"max_turns": -1},
              headers={"X-User-Id": "mtu4"})
    assert r.status_code == 422


# Appended to NimoOS-AI/agent/tests/test_main_max_turns.py
from unittest.mock import patch, MagicMock


def test_run_resolves_unlimited_and_passes_none():
    c = _client()
    user = "mtrun1"
    # user sets unlimited
    c.put("/agent/user-settings/max-turns", json={"max_turns": 0},
          headers={"X-User-Id": user})
    sid = c.post("/agent/sessions", headers={"X-User-Id": user}).json()["session_id"]

    captured = {}

    def fake_start_run(*args, **kwargs):
        captured.update(kwargs)
        # Mirror tests/test_run_with_attachments.py::_mock_sink: a sink whose
        # subscribe() yields a terminal 'done' so _stream_from_sink returns
        # immediately instead of live-tailing forever (which would hang the
        # TestClient reading the StreamingResponse body).
        sink = MagicMock()
        sink.is_done = False
        sink.subscribe.return_value = ([{"type": "done"}], MagicMock())
        return sink

    with patch("main._start_run", side_effect=fake_start_run):
        c.post(f"/agent/sessions/{sid}/run",
               json={"message": "hi", "model": "m"},
               headers={"X-User-Id": user, "X-Agent-Provider-Key": "k",
                        "X-Agent-Provider-Url": "http://x/v1"})
    assert captured.get("max_turns") is None  # 0 → unlimited → None


def test_continue_run_flag_forwarded():
    c = _client()
    user = "mtrun2"
    sid = c.post("/agent/sessions", headers={"X-User-Id": user}).json()["session_id"]
    captured = {}

    def fake_start_run(*args, **kwargs):
        captured.update(kwargs)
        # Mirror tests/test_run_with_attachments.py::_mock_sink: a sink whose
        # subscribe() yields a terminal 'done' so _stream_from_sink returns
        # immediately instead of live-tailing forever (which would hang the
        # TestClient reading the StreamingResponse body).
        sink = MagicMock()
        sink.is_done = False
        sink.subscribe.return_value = ([{"type": "done"}], MagicMock())
        return sink

    with patch("main._start_run", side_effect=fake_start_run):
        c.post(f"/agent/sessions/{sid}/run",
               json={"continue_run": True, "model": "m"},
               headers={"X-User-Id": user, "X-Agent-Provider-Key": "k",
                        "X-Agent-Provider-Url": "http://x/v1"})
    assert captured.get("continue_run") is True
