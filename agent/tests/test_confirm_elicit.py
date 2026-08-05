"""ConfirmManager 的三态扩展,以及"答案不落盘"这条硬规则。

规范禁止服务端用 form 模式索取密码 / API key / token，但不合规的服务端正是我们要防的
东西。所以：问题持久化（卡片要能活过重连），答案只存在内存里，读一次就没。
"""
import asyncio
import sqlite3

import pytest

import confirm as C
from confirm import ConfirmManager


def _mgr(timeout=5):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE pending_confirmations (confirm_id TEXT, session_id TEXT, "
               "action TEXT, description TEXT, command TEXT, created_at INT)")
    return ConfirmManager(db, timeout=timeout), db


def test_the_three_actions_are_the_spec_ones():
    from mcp.types import ElicitResult
    assert C.ELICIT_ACTIONS == ("accept", "decline", "cancel")
    # 钉在真实 SDK 模型上：Literal 变了这里必须红，不能等到生产里 ValidationError
    for a in C.ELICIT_ACTIONS:
        assert ElicitResult(action=a).action == a


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["accept", "decline", "cancel"])
async def test_wait_elicit_returns_the_action_the_user_chose(action):
    mgr, _ = _mgr()
    cid = mgr.register("s1", "mcp_elicit:1", "server asks", "What is your name?")
    payload = {"name": "Nimo"} if action == "accept" else None

    async def answer():
        await asyncio.sleep(0)
        mgr.resolve(cid, action == "accept", action=action, content=payload)

    got, _ = await asyncio.gather(mgr.wait_elicit(cid), answer())
    assert got == (action, payload)


@pytest.mark.asyncio
async def test_timeout_is_cancel_not_decline():
    """decline = 用户说不要；cancel = 用户没作声。对服务端是不同的信息。"""
    mgr, _ = _mgr(timeout=0.05)
    cid = mgr.register("s1", "mcp_elicit:1", "d", "q")
    assert await mgr.wait_elicit(cid) == ("cancel", None)


@pytest.mark.asyncio
async def test_unknown_id_is_cancel():
    mgr, _ = _mgr()
    assert await mgr.wait_elicit("nope") == ("cancel", None)


@pytest.mark.asyncio
async def test_cancel_session_resolves_an_elicitation_as_cancel():
    mgr, _ = _mgr()
    cid = mgr.register("s1", "mcp_elicit:1", "d", "q")

    async def kill():
        await asyncio.sleep(0)
        assert mgr.cancel_session("s1") == 1

    got, _ = await asyncio.gather(mgr.wait_elicit(cid), kill())
    assert got == ("cancel", None)


def test_resolve_rejects_an_action_outside_the_三态():
    mgr, _ = _mgr()
    cid = mgr.register("s1", "a", "d", "q")
    with pytest.raises(ValueError):
        mgr.resolve(cid, True, action="approve")


# ── 答案不落盘 ────────────────────────────────────────────────────────────────

def test_the_question_is_persisted_but_the_answer_never_is():
    mgr, db = _mgr()
    cid = mgr.register("s1", "mcp_elicit:1", "server asks", "What is your API key?")
    row = db.execute("SELECT command FROM pending_confirmations WHERE confirm_id=?",
                     (cid,)).fetchone()
    assert row["command"] == "What is your API key?"      # 问题在

    mgr.resolve(cid, True, action="accept", content={"key": "sk-SUPERSECRET"})
    dump = "".join(str(r) for r in
                   db.execute("SELECT * FROM pending_confirmations").fetchall())
    assert "SUPERSECRET" not in dump                       # 答案不在


def test_the_answer_never_reaches_the_audit_trail(monkeypatch):
    """_audit 写的是长期存在的审计日志。答案进去就等于落盘了。"""
    seen = []
    monkeypatch.setattr(C, "_audit", lambda ev, **kw: seen.append((ev, kw)))
    mgr, _ = _mgr()
    cid = mgr.register("s1", "mcp_elicit:1", "server asks", "What is your API key?")
    mgr.resolve(cid, True, action="accept", content={"key": "sk-SUPERSECRET"})

    assert seen, "audit was not called at all — the existing trail must not regress"
    blob = repr(seen)
    assert "SUPERSECRET" not in blob
    assert "content" not in blob
    assert seen[0][1]["decision"] == "accept"     # 三态如实记录,只是不带答案


@pytest.mark.asyncio
async def test_the_answer_is_read_once_and_gone():
    mgr, _ = _mgr()
    cid = mgr.register("s1", "mcp_elicit:1", "d", "q")

    async def answer():
        await asyncio.sleep(0)
        mgr.resolve(cid, True, action="accept", content={"a": "b"})

    got, _ = await asyncio.gather(mgr.wait_elicit(cid), answer())
    assert got == ("accept", {"a": "b"})
    assert mgr._contents == {} and mgr._actions == {}


@pytest.mark.asyncio
async def test_cancelling_the_waiter_does_not_leave_the_answer_in_memory():
    mgr, _ = _mgr(timeout=10)
    cid = mgr.register("s1", "mcp_elicit:1", "d", "q")
    mgr.resolve(cid, True, action="accept", content={"a": "b"})
    task = asyncio.create_task(mgr.wait_elicit(cid))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert mgr._contents == {} and mgr._actions == {}


# ── 既有二态路径不得回归 ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_existing_boolean_path_is_untouched():
    mgr, _ = _mgr()
    cid = mgr.register("s1", "shell", "d", "ls")

    async def answer():
        await asyncio.sleep(0)
        mgr.resolve(cid, True, remember=True)          # 不带 action

    ok, _ = await asyncio.gather(mgr.wait(cid), answer())
    assert ok is True
    assert mgr.consume_remember(cid) is True


# NOTE ON FIXTURE (deviation from the brief's inline snippet, not from its intent):
# main.py holds a module-level StreamableHTTPSessionManager singleton whose .run()
# "can only be called once per instance" (verified empirically). Opening a fresh
# `with TestClient(main_module.app) as client:` in each test function starts that
# lifespan twice in one process and the second one raises RuntimeError. The brief's
# own prose says to get the app "the way tests/test_mcp_server_e2e.py does it" —
# that file dodges exactly this by entering the TestClient context once behind a
# session-scoped fixture. Reusing that pattern here; test bodies/assertions below
# are otherwise verbatim.
@pytest.fixture(scope="session")
def _confirm_endpoint_client():
    from fastapi.testclient import TestClient
    import main as main_module
    with TestClient(main_module.app) as c:
        yield c, main_module


def test_confirm_endpoint_passes_action_and_content_through(monkeypatch, _confirm_endpoint_client):
    client, main_module = _confirm_endpoint_client

    calls = []
    monkeypatch.setattr(main_module._confirm_mgr, "resolve",
                        lambda *a, **k: calls.append((a, k)))
    r = client.post("/agent/sessions/s1/confirm",
                    headers={"X-User-Id": "1"},
                    json={"confirm_id": "c1", "confirmed": True,
                          "action": "accept", "content": {"name": "Nimo"}})
    assert r.status_code == 200
    assert calls[0][1]["action"] == "accept"
    assert calls[0][1]["content"] == {"name": "Nimo"}


def test_confirm_endpoint_rejects_an_unknown_action(monkeypatch, _confirm_endpoint_client):
    client, main_module = _confirm_endpoint_client

    monkeypatch.setattr(main_module._confirm_mgr, "resolve",
                        lambda *a, **k: pytest.fail("must not reach resolve"))
    r = client.post("/agent/sessions/s1/confirm",
                    headers={"X-User-Id": "1"},
                    json={"confirm_id": "c1", "action": "approve"})
    assert r.status_code == 400
