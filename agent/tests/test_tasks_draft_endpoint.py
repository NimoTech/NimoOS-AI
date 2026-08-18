# NimoOS-AI/agent/tests/test_tasks_draft_endpoint.py
"""M6 草稿端点。

**只读**:它读 sessions/messages,不写任何表。若将来有人在这里加了写操作,
`test_draft_writes_nothing` 会转红。

DB 隔离照 test_tasks_endpoints.py 的既有缝:monkeypatch `main._DB_PATH`,
绝不碰 `main._conn`(容器里那是生产库)。
"""
import asyncio
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


def _db_snapshot(conn):
    """Row counts for every table plus the full contents of the two tables
    this endpoint reads — an in-place UPDATE would not change a count."""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'").fetchall()]
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in tables}
    sessions = [tuple(r) for r in conn.execute("SELECT * FROM sessions").fetchall()]
    messages = [tuple(r) for r in conn.execute("SELECT * FROM messages").fetchall()]
    return counts, sessions, messages


def test_draft_writes_nothing(client):
    c, conn = client
    _seed_session(conn, "s1", "u1", [
        {"role": "user", "content": "hi"},
        {"type": "function_call", "name": "run_command",
         "arguments": '{"command": "gh pr list"}'},
    ])
    before = _db_snapshot(conn)
    r = c.post("/agent/tasks/draft-from-session", json={"session_id": "s1"}, headers=H)
    assert r.status_code == 200
    assert _db_snapshot(conn) == before


def test_bad_body_400(client):
    c, _ = client
    r = c.post("/agent/tasks/draft-from-session", json={}, headers=H)
    assert r.status_code == 400


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]


def _patch_model(monkeypatch, behavior):
    """Swap main.AsyncOpenAI for a stub whose completions.create runs `behavior`
    (which returns a _Resp or raises)."""
    class _Completions:
        async def create(self, **kwargs):
            return behavior(**kwargs)

    class _Chat:
        completions = _Completions()

    class _Client:
        def __init__(self, **kwargs):
            self.chat = _Chat()

    monkeypatch.setattr(main, "AsyncOpenAI", _Client)


def test_model_draft_wins_when_the_model_answers(client, monkeypatch):
    c, conn = client
    _seed_session(conn, "s1", "u1", [{"role": "user", "content": "拉数据"}])
    _patch_model(monkeypatch,
                 lambda **kw: _Resp('{"name": "汇总", "prompt": "每天拉数据并汇总"}'))
    body = c.post("/agent/tasks/draft-from-session",
                  json={"session_id": "s1", "model": "m"}, headers=H).json()
    assert body["prompt_fallback"] is False
    assert body["name"] == "汇总"
    assert body["prompt"] == "每天拉数据并汇总"


def test_unusable_model_output_falls_back(client, monkeypatch):
    c, conn = client
    _seed_session(conn, "s1", "u1", [{"role": "user", "content": "拉数据"}])
    _patch_model(monkeypatch, lambda **kw: _Resp("not json at all"))
    body = c.post("/agent/tasks/draft-from-session",
                  json={"session_id": "s1", "model": "m"}, headers=H).json()
    assert body["prompt_fallback"] is True
    assert body["prompt"] == "拉数据"


def test_model_exception_is_not_a_500(client, monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("upstream down")
    c, conn = client
    _seed_session(conn, "s1", "u1", [{"role": "user", "content": "拉数据"}])
    _patch_model(monkeypatch, _boom)
    r = c.post("/agent/tasks/draft-from-session",
               json={"session_id": "s1", "model": "m"}, headers=H)
    assert r.status_code == 200
    assert r.json()["prompt_fallback"] is True


def test_model_timeout_falls_back(client, monkeypatch):
    def _slow(**kwargs):
        raise asyncio.TimeoutError()
    c, conn = client
    _seed_session(conn, "s1", "u1", [{"role": "user", "content": "拉数据"}])
    _patch_model(monkeypatch, _slow)
    r = c.post("/agent/tasks/draft-from-session",
               json={"session_id": "s1", "model": "m"}, headers=H)
    assert r.status_code == 200
    assert r.json()["prompt_fallback"] is True


def test_non_dict_history_items_do_not_crash_the_model_branch(client, monkeypatch):
    # 回归用例:history 是数据库里的不可信 JSON。一串非 dict 元素曾让摘要
    # 抽取抛 AttributeError,变成 500 而不是文档承诺的兜底。
    c, conn = client
    _seed_session(conn, "s1", "u1", ["just a plain string", 42])
    _patch_model(monkeypatch, lambda **kw: _Resp('{"name":"n","prompt":"p"}'))
    r = c.post("/agent/tasks/draft-from-session",
               json={"session_id": "s1", "model": "m"}, headers=H)
    assert r.status_code == 200
    assert r.json()["prompt_fallback"] is True


def test_malformed_json_body_is_400_not_500(client):
    c, _ = client
    r = c.post("/agent/tasks/draft-from-session", data="{not json",
               headers={**H, "Content-Type": "application/json"})
    assert r.status_code == 400
