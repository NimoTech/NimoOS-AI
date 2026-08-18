# NimoOS-AI/agent/tests/test_tasks_draft_endpoint.py
"""M6 草稿端点。

**只读**:它读 sessions/messages,不写任何表。若将来有人在这里加了写操作,
`test_draft_writes_nothing` 会转红。

DB 隔离照 test_tasks_endpoints.py 的既有缝:monkeypatch `main._DB_PATH`,
绝不碰 `main._conn`(容器里那是生产库)。
"""
import json
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

import main

H = {"X-User-Id": "u1"}
H2 = {"X-User-Id": "u2"}


def _seed_session(conn, session_id, user_id, history):
    now = int(time.time())
    conn.execute(
        "INSERT INTO sessions (id, user_id, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?)", (session_id, user_id, "t", now, now))
    conn.execute(
        "INSERT INTO messages (id, session_id, role, content, created_at) "
        "VALUES (?,?,?,?,?)",
        (session_id + "-m", session_id, "history", json.dumps(history), now))
    conn.commit()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_DB_PATH", str(tmp_path / "agent.db"))
    conn = main._db()
    assert conn is not main._conn, (
        "draft endpoint tests must run against an isolated DB")
    yield TestClient(main.app), conn


def test_other_users_session_is_absent_not_forbidden(client):
    c, conn = client
    _seed_session(conn, "s1", "u2", [{"role": "user", "content": "hi"}])
    r = c.post("/agent/tasks/draft-from-session", json={"session_id": "s1"}, headers=H)
    assert r.status_code == 404


def test_missing_session_404(client):
    c, _ = client
    r = c.post("/agent/tasks/draft-from-session", json={"session_id": "nope"}, headers=H)
    assert r.status_code == 404


def test_no_model_falls_back_to_user_text(client):
    c, conn = client
    _seed_session(conn, "s1", "u1", [
        {"role": "user", "content": "拉昨天的销售数据"},
        {"type": "function_call", "name": "run_command",
         "arguments": '{"command": "lark-cli base record create --app x"}'},
        {"role": "user", "content": "写进飞书表格"},
    ])
    r = c.post("/agent/tasks/draft-from-session",
               json={"session_id": "s1", "model": ""}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["prompt_fallback"] is True
    assert body["prompt"] == "拉昨天的销售数据\n\n写进飞书表格"
    assert body["name"] == "拉昨天的销售数据"
    # 预授权反推与模型无关,兜底路径也必须给全
    assert body["preauth"]["shell"] == [
        {"kind": "prefix", "value": "lark-cli base record"}]


def test_egress_is_suggested_never_preauthorized(client):
    c, conn = client
    _seed_session(conn, "s1", "u1", [
        {"type": "function_call", "name": "run_command",
         "arguments": '{"command": "curl https://open.feishu.cn/api"}'},
    ])
    r = c.post("/agent/tasks/draft-from-session",
               json={"session_id": "s1"}, headers=H)
    body = r.json()
    assert body["preauth"]["egress_domains"] == []
    assert body["suggested_egress"] == ["open.feishu.cn"]


def test_preauth_shape_is_savable(client):
    """响应里的 preauth 必须能直接过 preauth.parse —— 否则 UI 一保存就 400。"""
    from tasks import preauth as _preauth
    c, conn = client
    _seed_session(conn, "s1", "u1", [
        {"type": "function_call", "name": "run_command",
         "arguments": '{"command": "gh pr list"}'},
        {"type": "function_call", "name": "write_file",
         "arguments": '{"path": "/DATA/Documents/r/a.md"}'},
    ])
    body = c.post("/agent/tasks/draft-from-session",
                  json={"session_id": "s1"}, headers=H).json()
    parsed = _preauth.parse(body["preauth"])
    assert parsed["shell"] == [{"kind": "prefix", "value": "gh pr list"}]
    assert parsed["fs_write"] == ["/DATA/Documents/r"]


def test_empty_session_yields_empty_draft_not_error(client):
    c, conn = client
    _seed_session(conn, "s1", "u1", [])
    r = c.post("/agent/tasks/draft-from-session", json={"session_id": "s1"}, headers=H)
    assert r.status_code == 200
    assert r.json()["prompt"] == ""


def test_draft_writes_nothing(client):
    c, conn = client
    _seed_session(conn, "s1", "u1", [{"role": "user", "content": "hi"}])
    before = conn.execute("SELECT COUNT(*) FROM scheduled_tasks").fetchone()[0]
    c.post("/agent/tasks/draft-from-session", json={"session_id": "s1"}, headers=H)
    after = conn.execute("SELECT COUNT(*) FROM scheduled_tasks").fetchone()[0]
    assert before == after == 0


def test_bad_body_400(client):
    c, _ = client
    r = c.post("/agent/tasks/draft-from-session", json={}, headers=H)
    assert r.status_code == 400
