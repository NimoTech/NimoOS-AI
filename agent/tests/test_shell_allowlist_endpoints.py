# agent/tests/test_shell_allowlist_endpoints.py
from fastapi.testclient import TestClient
import main


def _client():
    return TestClient(main.app)


def test_crud_roundtrip():
    c = _client()
    h = {"X-User-Id": "u1"}
    r = c.post("/agent/shell-allowlist",
               json={"match_type": "prefix", "value": "git pull", "note": "ok"},
               headers=h)
    assert r.status_code == 200
    eid = r.json()["id"]

    r = c.get("/agent/shell-allowlist", headers=h)
    assert r.status_code == 200
    assert any(e["id"] == eid for e in r.json()["entries"])

    r = c.delete(f"/agent/shell-allowlist/{eid}", headers=h)
    assert r.status_code == 200 and r.json()["ok"] is True


def test_bad_match_type_rejected():
    c = _client()
    r = c.post("/agent/shell-allowlist",
               json={"match_type": "nope", "value": "x"},
               headers={"X-User-Id": "u1"})
    assert r.status_code == 400
