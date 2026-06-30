# NimoOS-AI/agent/tests/test_mcp_token_routes.py
from fastapi.testclient import TestClient
import main

client = TestClient(main.app)
H = {"X-NimoOS-User-ID": "42"}


def test_requires_user_header():
    assert client.get("/mcp-tokens").status_code == 401
    assert client.post("/mcp-tokens", json={"label": "x"}).status_code == 401


def test_create_list_delete_flow():
    r = client.post("/mcp-tokens", json={"label": "laptop"}, headers=H)
    assert r.status_code == 201
    body = r.json()
    assert body["token"].startswith("nimoos_mcp_") and body["label"] == "laptop"
    tok_id = body["id"]

    r = client.get("/mcp-tokens", headers=H)
    assert r.status_code == 200
    ids = [t["id"] for t in r.json()["tokens"]]
    assert tok_id in ids
    assert "token" not in r.json()["tokens"][0]  # secret never listed

    r = client.delete(f"/mcp-tokens/{tok_id}", headers=H)
    assert r.json()["revoked"] is True
    assert tok_id not in [t["id"] for t in client.get("/mcp-tokens", headers=H).json()["tokens"]]
