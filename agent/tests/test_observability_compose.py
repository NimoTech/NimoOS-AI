from fastapi.testclient import TestClient
import main as mainmod


def test_observability_compose_endpoint():
    client = TestClient(mainmod.app)
    r = client.get("/agent/observability/compose")
    assert r.status_code == 200
    body = r.text
    assert "arize-phoenix" in body
    assert "arizephoenix/phoenix" in body
    assert "x-nimoos" in body
    assert "6006" in body
