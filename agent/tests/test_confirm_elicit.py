"""ConfirmManager 的三态扩展,以及"答案不落盘"这条硬规则。

规范禁止服务端用 form 模式索取密码 / API key / token，但不合规的服务端正是我们要防的
东西。所以：问题持久化（卡片要能活过重连），答案只存在内存里，读一次就没。
"""
import asyncio
import sqlite3

import anyio
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


# ── wait_elicit() 在真实 anyio task group 里的行为 ──────────────────────────────
#
# Task 4 will call wait_elicit from inside the MCP SDK's own dispatch loop, which
# runs under anyio task groups (the SDK depends on anyio for exactly this), not a
# bare asyncio.create_task(). Task.cancelling() is a whole-TASK counter, not scoped
# to a single await — the risk the reviewer named is that an unrelated outer
# cancellation (e.g. a sibling in the same task group, or a scope that already
# fired and was handled elsewhere in the same task) could leave the counter
# nonzero and make wait_elicit's guard (confirm.py's Task.cancelling() check,
# added because plain asyncio.wait_for swallows a same-tick CancelledError — see
# the comment on wait_elicit) misfire and turn a legitimately-obtained answer into
# a spurious CancelledError. These two tests pin that anyio's asyncio backend does
# NOT leave that stale state behind: a normal completion inside a live task group
# returns the answer without raising, and an actual cancel-scope cancellation both
# propagates AND leaves no answer in memory.

@pytest.mark.asyncio
async def test_wait_elicit_completes_normally_inside_a_live_anyio_task_group():
    mgr, _ = _mgr()
    cid = mgr.register("s1", "mcp_elicit:1", "d", "q")
    result = {}

    async def waiter():
        result["got"] = await mgr.wait_elicit(cid)

    async def answer():
        await anyio.sleep(0)
        mgr.resolve(cid, True, action="accept", content={"a": "b"})

    # No cancellation anywhere in this test. If Task.cancelling() ever reported
    # a stale nonzero count on a task that merely happens to run inside an anyio
    # task group, this would spuriously raise CancelledError instead of returning.
    async with anyio.create_task_group() as tg:
        tg.start_soon(waiter)
        tg.start_soon(answer)

    assert result["got"] == ("accept", {"a": "b"})


@pytest.mark.asyncio
async def test_wait_elicit_cancelled_by_a_task_group_scope_propagates_and_clears_memory():
    mgr, _ = _mgr(timeout=10)
    cid = mgr.register("s1", "mcp_elicit:1", "d", "q")
    # Never resolved: the waiter is still genuinely pending when the scope below
    # cancels it, which is the real-world shape of "user closed the SSE stream" /
    # "session cancelled" while a form is still outstanding.

    async def waiter():
        await mgr.wait_elicit(cid)

    with anyio.move_on_after(0.05):
        async with anyio.create_task_group() as tg:
            tg.start_soon(waiter)
            await anyio.sleep(3600)  # outlives the 0.05s deadline; scope cancels both

    assert mgr._actions == {} and mgr._contents == {}


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


# NOTE ON TestClient (deviation from the brief's inline snippet, not from its intent):
# main.py holds a module-level StreamableHTTPSessionManager singleton whose .run()
# "can only be called once per instance" (verified empirically). Entering
# `with TestClient(main_module.app) as client:` runs the app's ASGI lifespan
# (startup/shutdown), and a *second* such block anywhere else in the same test
# process — including tests/test_mcp_server_e2e.py's own session-scoped `client`
# fixture, which also wraps main.app — hits that guard and raises RuntimeError,
# regardless of run order. `/confirm` and `_confirm_mgr` are fully initialised at
# import time (main.py:56-57), not by a startup hook, so the endpoint under test
# needs none of the lifespan machinery. Constructing TestClient WITHOUT entering
# it as a context manager sidesteps the lifespan entirely (verified: two separate
# such instances used back-to-back do not collide), so that's what these two tests
# do. Everything else — requests sent, bodies, assertions — is verbatim.
def test_confirm_endpoint_passes_action_and_content_through(monkeypatch):
    from fastapi.testclient import TestClient
    import main as main_module

    calls = []
    monkeypatch.setattr(main_module._confirm_mgr, "resolve",
                        lambda *a, **k: calls.append((a, k)))
    client = TestClient(main_module.app)
    r = client.post("/agent/sessions/s1/confirm",
                    headers={"X-User-Id": "1"},
                    json={"confirm_id": "c1", "confirmed": True,
                          "action": "accept", "content": {"name": "Nimo"}})
    assert r.status_code == 200
    assert calls[0][1]["action"] == "accept"
    assert calls[0][1]["content"] == {"name": "Nimo"}


def test_confirm_endpoint_rejects_an_unknown_action(monkeypatch):
    from fastapi.testclient import TestClient
    import main as main_module

    monkeypatch.setattr(main_module._confirm_mgr, "resolve",
                        lambda *a, **k: pytest.fail("must not reach resolve"))
    client = TestClient(main_module.app)
    r = client.post("/agent/sessions/s1/confirm",
                    headers={"X-User-Id": "1"},
                    json={"confirm_id": "c1", "action": "approve"})
    assert r.status_code == 400


# ── per-call 超时与超时语义（URL 授权卡需要"走开了也把 accept 发出去"）─────────

@pytest.mark.asyncio
async def test_wait_elicit_honours_a_per_call_timeout_shorter_than_the_managers():
    """URL 授权卡等 3 分钟,表单卡等 24 小时,两者共用一个 ConfirmManager。"""
    mgr, _ = _mgr(timeout=3600)
    cid = mgr.register("s1", "mcp_elicit:1", "d", "q")
    assert await mgr.wait_elicit(cid, timeout=0.05) == ("cancel", None)


@pytest.mark.asyncio
async def test_on_timeout_lets_the_caller_choose_what_a_timeout_means():
    """策略属于调用方:URL 卡要的是"用户走开了也把 accept 发出去"(让服务端有机会
    长轮询或回 state-only),普通卡要的是 cancel。confirm.py 只提供开关。"""
    mgr, _ = _mgr(timeout=3600)
    cid = mgr.register("s1", "mcp_elicit:1", "d", "q")
    assert await mgr.wait_elicit(cid, timeout=0.05, on_timeout="accept") == ("accept", None)


@pytest.mark.asyncio
async def test_an_explicit_user_answer_still_wins_over_on_timeout():
    """on_timeout 只在真的超时时生效 —— 用户点了取消就是取消,不能被"超时算 accept"
    覆盖成"同意"。"""
    mgr, _ = _mgr(timeout=3600)
    cid = mgr.register("s1", "mcp_elicit:1", "d", "q")

    async def answer():
        await asyncio.sleep(0)
        mgr.resolve(cid, False, action="cancel")

    got, _ = await asyncio.gather(
        mgr.wait_elicit(cid, timeout=5, on_timeout="accept"), answer())
    assert got == ("cancel", None)


@pytest.mark.asyncio
async def test_a_bogus_on_timeout_fails_fast_instead_of_three_minutes_later():
    """timeout=3600 是故意的:如果校验放在等待之后,这个测试会挂一小时而不是立刻红。"""
    mgr, _ = _mgr(timeout=3600)
    cid = mgr.register("s1", "mcp_elicit:1", "d", "q")
    with pytest.raises(ValueError):
        await mgr.wait_elicit(cid, timeout=3600, on_timeout="approve")
