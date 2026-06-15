from fastapi.testclient import TestClient
import main


def test_healthz_ok():
    client = TestClient(main.app)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
