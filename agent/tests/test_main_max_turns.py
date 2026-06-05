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
