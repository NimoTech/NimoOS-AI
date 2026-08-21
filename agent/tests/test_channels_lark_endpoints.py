# NimoOS-AI/agent/tests/test_channels_lark_endpoints.py
"""M4 第一段:飞书 channel 的三个端点。

`/agent/channels/instances` 已经是 admin-scoped(route/v2/admin_guard.go 的
AdminScopedAgentPaths),但**本组端点的路径不在那棵子树内**,所以它们的 admin
归属必须单独确认 —— 启用一个 channel 是给整台机器配机器人凭据,属于管理动作。
本文件只测 Python 层的用户作用域;Go 侧的闸门在 Task 4 的 Step 5 单独验。
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

import main
from channels import lark_setup

H = {"X-User-Id": "1"}
H2 = {"X-User-Id": "2"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_DB_PATH", str(tmp_path / "agent.db"))
    conn = main._db()
    assert conn is not main._conn

    async def _fake_identity(uid):
        return {"open_id": f"ou_{uid}", "name": "Tester"}

    monkeypatch.setattr(lark_setup, "resolve_bot_identity", _fake_identity)
    yield TestClient(main.app), conn


def test_status_is_disabled_before_setup(client):
    c, _ = client
    body = c.get("/agent/channels/lark", headers=H).json()
    assert body["enabled"] is False
    assert body["instance_id"] == ""


def test_enable_then_status_reports_the_identity(client):
    c, _ = client
    body = c.post("/agent/channels/lark", headers=H).json()
    assert body["enabled"] is True
    assert body["open_id"] == "ou_1"

    again = c.get("/agent/channels/lark", headers=H).json()
    assert again["instance_id"] == body["instance_id"]


def test_enable_is_scoped_per_user(client):
    c, _ = client
    c.post("/agent/channels/lark", headers=H)
    other = c.get("/agent/channels/lark", headers=H2).json()
    assert other["enabled"] is False


def test_enable_reports_a_clear_error_when_the_cli_is_unusable(client, monkeypatch):
    c, _ = client

    async def _none(uid):
        return None

    monkeypatch.setattr(lark_setup, "resolve_bot_identity", _none)
    r = c.post("/agent/channels/lark", headers=H)
    assert r.status_code == 409
    assert r.json()["detail"] == "lark_unavailable"


def test_disable_is_204_and_idempotent(client):
    c, _ = client
    c.post("/agent/channels/lark", headers=H)
    assert c.delete("/agent/channels/lark", headers=H).status_code == 204
    assert c.delete("/agent/channels/lark", headers=H).status_code == 204
    assert c.get("/agent/channels/lark", headers=H).json()["enabled"] is False


def test_pairable_instances_never_offer_a_lark_instance(monkeypatch):
    """Task 3 review: a Lark instance has no inbound path — this milestone
    consumes only card clicks, never messages — so it must never be offered
    as a target for /agent/channels/pairing-code. A Telegram instance created
    the normal way is unaffected.

    Deliberately does NOT use the `client` fixture above: that fixture
    monkeypatches `_DB_PATH` so `lark_setup.enable()` (which reads via
    `_db()`) gets an isolated tmp-file connection, but
    `/agent/channels/pairable-instances` still reads the module-level
    `_conn` directly (untouched by that monkeypatch) — the two would then
    disagree about what exists. Running both through the shared,
    conftest-provided `:memory:` `_conn` keeps this a same-DB comparison
    without ever assigning to `main._conn` from test code.
    """
    from channels.telegram import TelegramAdapter

    async def _fake_identity(uid):
        return {"open_id": f"ou_{uid}", "name": "Tester"}

    monkeypatch.setattr(lark_setup, "resolve_bot_identity", _fake_identity)

    async def _fake_validate(token, *, transport=None):
        return {"bot_username": "nimo_bot"} if token == "good:token" else None

    monkeypatch.setattr(TelegramAdapter, "validate_token",
                        staticmethod(_fake_validate))
    monkeypatch.setattr(main, "_channel_manager", None)

    c = TestClient(main.app)
    lark_id = c.post("/agent/channels/lark", headers=H).json()["instance_id"]
    tg_id = c.post("/agent/channels/instances", headers=H, json={
        "channel_type": "telegram", "name": "TG Bot",
        "config": {"bot_token": "good:token"}}).json()["id"]

    body = c.get("/agent/channels/pairable-instances", headers=H).json()
    ids = [i["id"] for i in body["instances"]]
    assert lark_id not in ids
    assert tg_id in ids
