"""GET/PUT /agent/web-settings and the egress-confirm auto-approve path."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main as main_mod
from web import settings as web_settings


@pytest.fixture
def client():
    return TestClient(main_mod.app)


def test_get_returns_defaults_and_never_the_key(client):
    r = client.get("/agent/web-settings")
    assert r.status_code == 200
    body = r.json()
    assert body == {"backend": "", "base_url": "", "enabled": False,
                    "has_key": False}
    assert "api_key" not in body


def test_put_saves_and_masks(client):
    r = client.put("/agent/web-settings", json={
        "backend": "tavily", "api_key": "tvly-secret",
        "base_url": "", "enabled": True})
    assert r.status_code == 200
    assert r.json() == {"backend": "tavily", "base_url": "",
                        "enabled": True, "has_key": True}
    assert "tvly-secret" not in r.text


def test_put_without_api_key_keeps_the_stored_one(client):
    client.put("/agent/web-settings", json={
        "backend": "tavily", "api_key": "tvly-secret",
        "base_url": "", "enabled": True})
    r = client.put("/agent/web-settings", json={
        "backend": "tavily", "base_url": "", "enabled": False})
    assert r.status_code == 200
    assert r.json()["has_key"] is True
    assert web_settings.load(main_mod._db())["api_key"] == "tvly-secret"


def test_put_rejects_unknown_backend(client):
    r = client.put("/agent/web-settings", json={
        "backend": "nope", "base_url": "", "enabled": True})
    assert r.status_code == 400


def test_egress_confirm_auto_approves_the_configured_backend_host(client):
    web_settings.save(main_mod._db(), backend="tavily", api_key="k",
                      base_url="", enabled=True)
    r = client.post("/internal/egress-confirm",
                    json={"host": "api.tavily.com", "bytes": 0,
                          "reason": "tofu_unknown_host"})
    assert r.status_code == 200
    assert r.json() == {"allow": True}


def test_egress_confirm_still_fails_closed_for_other_hosts(client):
    web_settings.save(main_mod._db(), backend="tavily", api_key="k",
                      base_url="", enabled=True)
    # No active session, so the normal path fail-closes — the point is that
    # the auto-approve branch did NOT swallow an unrelated host.
    r = client.post("/internal/egress-confirm",
                    json={"host": "evil.test", "bytes": 0,
                          "reason": "tofu_unknown_host"})
    assert r.json() == {"allow": False}


def test_egress_confirm_no_auto_approve_when_disabled(client):
    web_settings.save(main_mod._db(), backend="tavily", api_key="k",
                      base_url="", enabled=False)
    r = client.post("/internal/egress-confirm",
                    json={"host": "api.tavily.com", "bytes": 0,
                          "reason": "tofu_unknown_host"})
    assert r.json() == {"allow": False}
