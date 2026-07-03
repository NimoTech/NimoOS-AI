# NimoOS-AI/agent/tests/test_channels_endpoints.py
import pytest
from fastapi.testclient import TestClient

import main
from channels import store
from channels.telegram import TelegramAdapter

H = {"X-User-Id": "u1"}


@pytest.fixture
def client(monkeypatch):
    async def fake_validate(token, *, transport=None):
        return {"bot_username": "nimo_bot"} if token == "good:token" else None
    monkeypatch.setattr(TelegramAdapter, "validate_token",
                        staticmethod(fake_validate))
    with TestClient(main.app) as c:
        # Neutralize the manager so instance writes never start a REAL
        # TelegramAdapter long-polling api.telegram.org from inside tests.
        monkeypatch.setattr(main, "_channel_manager", None)
        yield c


def test_instance_create_list_toggle_delete(client):
    r = client.post("/agent/channels/instances", headers=H, json={
        "channel_type": "telegram", "name": "family",
        "config": {"bot_token": "good:token"}})
    assert r.status_code == 201
    body = r.json()
    assert body["bot_username"] == "nimo_bot"
    assert body["token_tail"] == "oken" and "bot_token" not in body
    iid = body["id"]
    r = client.get("/agent/channels/instances", headers=H)
    assert iid in [i["id"] for i in r.json()["instances"]]
    assert client.put(f"/agent/channels/instances/{iid}", headers=H,
                      json={"enabled": False}).json() == {"ok": True}
    assert client.delete(f"/agent/channels/instances/{iid}",
                         headers=H).json() == {"ok": True}


def test_instance_create_rejects_bad_token_and_type(client):
    r = client.post("/agent/channels/instances", headers=H, json={
        "channel_type": "telegram", "config": {"bot_token": "bad"}})
    assert r.status_code == 422
    r = client.post("/agent/channels/instances", headers=H, json={
        "channel_type": "martian", "config": {}})
    assert r.status_code == 422


def test_pairing_and_binding_flow(client):
    iid = client.post("/agent/channels/instances", headers=H, json={
        "channel_type": "telegram",
        "config": {"bot_token": "good:token"}}).json()["id"]
    r = client.post("/agent/channels/pairing-code", headers=H,
                    json={"instance_id": iid})
    assert r.status_code == 201
    code = r.json()["code"]
    assert len(code) == 8
    # redeem directly through the store (simulates /pair from telegram)
    b = store.redeem_pairing_code(main._conn, iid, code, "tg9", "alice",
                                  now_ms=0)
    r = client.get("/agent/channels/bindings", headers=H)
    rows = r.json()["bindings"]
    assert rows[0]["id"] == b["id"] and rows[0]["channel_type"] == "telegram"
    assert client.put(f"/agent/channels/bindings/{b['id']}/model", headers=H,
                      json={"model": "qwen3"}).json() == {"ok": True}
    assert client.delete(f"/agent/channels/bindings/{b['id']}",
                         headers=H).json() == {"revoked": True}
    # other user sees nothing / cannot revoke
    r = client.get("/agent/channels/bindings", headers={"X-User-Id": "u2"})
    assert r.json()["bindings"] == []


def test_pairing_code_requires_valid_instance(client):
    r = client.post("/agent/channels/pairing-code", headers=H,
                    json={"instance_id": "nope"})
    assert r.status_code == 404
