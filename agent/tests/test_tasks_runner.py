"""Runner — claim a queued task_run, build its session, inject the task's
pre-authorization, drive the run headlessly, record the result.

Every side effect is injected (`start_run`, `creds_resolver`, `driver_factory`,
`session_factory`, `grant_fs`, `grant_egress`), the same shape as
`notes_distill.process_pending_once`, so nothing here touches an LLM, the
egress-proxy or a real agent run.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import asyncio
from unittest import mock

import pytest

import db as db_module
from notes import store as notes_store
from tasks import runner, store

NOW = 1_800_000_000


@pytest.fixture
def conn(tmp_path):
    return db_module.init_db(str(tmp_path / "t.db"))


# -- fakes -------------------------------------------------------------------

class FakeSink:
    """Stand-in for main.RunSink. `task` is the agent coroutine's task, which
    is what the timeout path has to cancel."""

    def __init__(self, task=None):
        self.task = task


class FakeDriver:
    def __init__(self, result, *, session_id="", preauth=None, run_timeout=0,
                 raises=None, task=None):
        self.result = result
        self.session_id = session_id
        self.preauth = preauth
        self.run_timeout = run_timeout
        self.raises = raises
        self.task = task
        self.driven = None

    async def drive(self, sink):
        self.driven = sink
        if self.raises is not None:
            raise self.raises
        return self.result


class Harness:
    """Collects every injected call so a test can assert on the wiring."""

    def __init__(self, result=None, *, creds=None, driver_raises=None):
        self.result = result if result is not None else {
            "status": "succeeded", "summary": "all good", "error": "",
            "denied": [], "auto_approved": [],
        }
        self.creds = creds if creds is not None else {
            "api_key": "sk-secret", "base_url": "http://ollama", "model": "qwen3",
        }
        self.driver_raises = driver_raises
        self.start_run_calls = []
        self.creds_calls = []
        self.fs_calls = []
        self.egress_calls = []
        self.sessions = []
        self.driver = None
        self.sink = FakeSink()
        self.cancelled = []
        self.evicted = []
        self.deleted = []
        self.workspaces = []
        self.workspace_path = ""      # "" = this task has no working folder

    # start_run(session_id, user_id, message, creds, *, max_turns,
    #           pre_confirmed_tools, run_shell_allowlist) -> sink
    def start_run(self, session_id, user_id, message, creds, **kw):
        self.start_run_calls.append(
            {"session_id": session_id, "user_id": user_id, "message": message,
             "creds": creds, **kw})
        return self.sink

    async def creds_resolver(self, user_id, model):
        self.creds_calls.append((user_id, model))
        return self.creds

    def driver_factory(self, **kw):
        self.driver = FakeDriver(self.result, raises=self.driver_raises, **kw)
        return self.driver

    def session_factory(self, conn, user_id, agent_type):
        sid = f"sess-{len(self.sessions)}"
        conn.execute(
            "INSERT INTO sessions (id, user_id, title, created_at, updated_at, "
            "agent_type, source) VALUES (?,?,?,?,?,?,?)",
            (sid, user_id, None, NOW, NOW, agent_type, "task"))
        conn.commit()
        self.sessions.append((user_id, agent_type))
        return sid

    def grant_fs(self, conn, session_id, paths):
        self.fs_calls.append((session_id, list(paths)))
        return len(paths)

    async def grant_egress(self, domains):
        self.egress_calls.append(list(domains))
        return {d: True for d in domains}

    async def cancel(self, sink):
        self.cancelled.append(sink)
        return True

    def evict(self, session_id, sink=None):
        self.evicted.append((session_id, sink))

    async def session_deleter(self, conn, user_id, session_id):
        self.deleted.append((user_id, session_id))

    def workspace_ensure(self, task_id, name=""):
        self.workspaces.append((task_id, name))
        return self.workspace_path

    async def run(self, conn, **over):
        kw = dict(start_run=self.start_run, creds_resolver=self.creds_resolver,
                  driver_factory=self.driver_factory,
                  session_factory=self.session_factory,
                  grant_fs=self.grant_fs, grant_egress=self.grant_egress,
                  cancel=self.cancel, evict=self.evict,
                  session_deleter=self.session_deleter,
                  # No workspace unless a test asks for one. The real default
                  # creates a directory under /DATA, and before this seam
                  # existed one run of this file left 58 of them on the live box.
                  workspace_ensure=self.workspace_ensure,
                  now=NOW)
        kw.update(over)
        return await runner.process_once(conn, **kw)


def _mk(conn, **over):
    kw = dict(name="daily", prompt="write the report", trigger_type="cron",
              cron_expr="0 9 * * *", agent_type="general", model="qwen3",
              max_turns=7, timeout_seconds=900, preauth={})
    kw.update(over)
    return store.create_task(conn, "u1", **kw)


def _queue(conn, task_id, trigger="cron"):
    return store.create_run(conn, task_id, "u1", trigger)


def _run_row(conn, run_id):
    return conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()


# -- happy path --------------------------------------------------------------

@pytest.mark.asyncio
async def test_nothing_queued_returns_false(conn):
    assert await Harness().run(conn) is False


@pytest.mark.asyncio
async def test_successful_run_is_recorded(conn):
    tid = _mk(conn)
    rid = _queue(conn, tid)
    h = Harness({"status": "succeeded", "summary": "done", "error": "",
                 "denied": [], "auto_approved": []})

    assert await h.run(conn) is True

    row = _run_row(conn, rid)
    assert row["status"] == "succeeded"
    assert row["summary"] == "done"
    assert row["error"] == ""
    assert row["finished_at"] > 0
    # Session was created, attached to the run and handed to start_run.
    assert row["session_id"] == "sess-0"
    assert h.start_run_calls[0]["session_id"] == "sess-0"
    assert h.start_run_calls[0]["message"] == "write the report"
    assert h.start_run_calls[0]["creds"] == h.creds
    assert h.driver.driven is h.sink


@pytest.mark.asyncio
async def test_task_model_wins_over_the_background_model(conn):
    tid = _mk(conn, model="cloud:p1:gpt")
    _queue(conn, tid)
    notes_store.set_background_model(conn, "u1", "fallback-model")
    h = Harness()

    await h.run(conn)
    assert h.creds_calls == [("u1", "cloud:p1:gpt")]


@pytest.mark.asyncio
async def test_background_model_is_the_fallback(conn):
    tid = _mk(conn, model="")
    _queue(conn, tid)
    notes_store.set_background_model(conn, "u1", "fallback-model")
    h = Harness()

    await h.run(conn)
    assert h.creds_calls == [("u1", "fallback-model")]


@pytest.mark.asyncio
async def test_agent_type_and_timeout_reach_the_session_and_driver(conn):
    tid = _mk(conn, agent_type="coder", timeout_seconds=123)
    _queue(conn, tid)
    h = Harness()

    await h.run(conn)
    assert h.sessions == [("u1", "coder")]
    assert h.driver.run_timeout == 123
    assert h.driver.session_id == "sess-0"


@pytest.mark.asyncio
async def test_task_max_turns_is_passed_through(conn):
    tid = _mk(conn, max_turns=7)
    _queue(conn, tid)
    h = Harness()
    await h.run(conn)
    assert h.start_run_calls[0]["max_turns"] == 7


@pytest.mark.asyncio
async def test_zero_max_turns_means_unlimited(conn):
    tid = _mk(conn, max_turns=0)
    _queue(conn, tid)
    h = Harness()
    await h.run(conn)
    assert h.start_run_calls[0]["max_turns"] is None


# -- failure paths -----------------------------------------------------------

@pytest.mark.asyncio
async def test_no_model_configured_fails_the_run_loudly(conn):
    tid = _mk(conn, model="")
    rid = _queue(conn, tid)
    h = Harness()

    assert await h.run(conn) is True

    row = _run_row(conn, rid)
    assert row["status"] == "failed"
    assert row["error"] == "no model configured"
    # Nothing was started: no session, no agent run.
    assert h.sessions == [] and h.start_run_calls == []
    assert row["session_id"] == ""


@pytest.mark.asyncio
async def test_deleted_task_fails_the_orphaned_run(conn):
    tid = _mk(conn)
    rid = _queue(conn, tid)
    # The task row is dropped WITHOUT store.delete_task, which now takes the
    # queued run with it. This is the residual race the branch still guards:
    # a run claimed just before the delete lands (or a hand-edited DB).
    conn.execute("DELETE FROM scheduled_tasks WHERE id=?", (tid,))
    conn.commit()
    h = Harness()

    assert await h.run(conn) is True

    row = _run_row(conn, rid)
    assert row["status"] == "failed"
    assert row["error"]
    assert h.start_run_calls == []


@pytest.mark.asyncio
async def test_unresolved_credentials_fail_the_run(conn):
    tid = _mk(conn)
    rid = _queue(conn, tid)
    h = Harness(creds=None)
    h.creds = None

    assert await h.run(conn) is True
    row = _run_row(conn, rid)
    assert row["status"] == "failed"
    assert "credential" in row["error"].lower()
    assert h.start_run_calls == []


@pytest.mark.asyncio
async def test_driver_timeout_is_recorded_verbatim(conn):
    tid = _mk(conn)
    rid = _queue(conn, tid)
    h = Harness({"status": "timeout", "summary": "partial work",
                 "error": "timeout", "denied": [{"kind": "shell", "detail": "rm -rf /"}],
                 "auto_approved": []})

    await h.run(conn)
    row = _run_row(conn, rid)
    assert row["status"] == "timeout"
    assert row["summary"] == "partial work"
    assert row["error"] == "timeout"
    assert '"shell"' in row["denied_actions"]


@pytest.mark.asyncio
async def test_driver_exception_never_escapes(conn):
    tid = _mk(conn)
    rid = _queue(conn, tid)
    h = Harness(driver_raises=RuntimeError("boom"))

    assert await h.run(conn) is True            # must not raise

    row = _run_row(conn, rid)
    assert row["status"] == "failed"
    assert "boom" in row["error"]


@pytest.mark.asyncio
async def test_api_key_is_never_written_into_the_error(conn):
    tid = _mk(conn)
    rid = _queue(conn, tid)
    h = Harness(driver_raises=RuntimeError("auth failed for sk-secret"))

    await h.run(conn)
    row = _run_row(conn, rid)
    assert "sk-secret" not in row["error"]


@pytest.mark.asyncio
async def test_session_factory_failure_does_not_leave_the_run_running(conn):
    tid = _mk(conn)
    rid = _queue(conn, tid)
    h = Harness()

    def boom(*a, **kw):
        raise OSError("disk full")

    assert await h.run(conn, session_factory=boom) is True
    assert _run_row(conn, rid)["status"] == "failed"


# -- pre-authorization injection --------------------------------------------

PREAUTH = {
    "shell": [{"kind": "prefix", "value": "lark-cli "},
              {"kind": "regex", "value": "^rm .*"}],      # regex is dropped
    "egress_domains": ["open.feishu.cn"],
    "mcp_tools": ["srv1::send_message"],
    "fs_write": ["/DATA/reports"],
}


@pytest.mark.asyncio
async def test_all_four_preauth_channels_are_wired(conn):
    tid = _mk(conn, preauth=PREAUTH)
    _queue(conn, tid)
    h = Harness()

    await h.run(conn)

    # 1. fs — session-scoped, with the parsed paths
    assert h.fs_calls == [("sess-0", ["/DATA/reports"])]
    # 2. egress — bare domains (grants.grant_egress strips ports itself)
    assert h.egress_calls == [["open.feishu.cn"]]
    # 3. MCP tools — passed through verbatim as a set
    assert h.start_run_calls[0]["pre_confirmed_tools"] == {"srv1::send_message"}
    # 4. shell — only the prefix rule survives preauth.parse
    assert h.start_run_calls[0]["run_shell_allowlist"] == [
        {"kind": "prefix", "value": "lark-cli "}]
    # And the driver decides confirmation cards from the same parsed document.
    assert h.driver.preauth["egress_domains"] == ["open.feishu.cn"]


@pytest.mark.asyncio
async def test_grants_run_after_the_session_is_attached(conn):
    """fs grants are session-scoped, so the run must already own its session
    row — otherwise a failure between the two would leave visible_resources
    pointing at a session nobody can find."""
    tid = _mk(conn, preauth=PREAUTH)
    rid = _queue(conn, tid)
    order = []
    attached = []
    h = Harness()

    def fs(conn_, sid, paths):
        order.append("fs")
        # Recorded, not asserted: process_once swallows exceptions from the
        # grant helpers (a failed grant must not sink the run), so an assert
        # in here would be invisible.
        attached.append((_run_row(conn_, rid)["session_id"], sid))
        return len(paths)

    h.grant_fs = fs

    async def eg(domains):
        order.append("egress")
        return {}

    h.grant_egress = eg

    def start(*a, **kw):
        order.append("start_run")
        return h.sink

    await h.run(conn, grant_fs=fs, grant_egress=eg, start_run=start)
    assert order == ["fs", "egress", "start_run"]
    assert attached and attached[0][0] == attached[0][1] != ""


@pytest.mark.asyncio
async def test_egress_grant_failure_does_not_abort_the_run(conn):
    tid = _mk(conn, preauth=PREAUTH)
    rid = _queue(conn, tid)
    h = Harness()

    async def eg(domains):
        raise OSError("proxy down")

    await h.run(conn, grant_egress=eg)
    # The proxy's own confirm flow is the fallback; the run still happens.
    assert _run_row(conn, rid)["status"] == "succeeded"


@pytest.mark.asyncio
async def test_auto_approved_actions_are_appended_to_the_summary(conn):
    tid = _mk(conn, preauth=PREAUTH)
    rid = _queue(conn, tid)
    h = Harness({"status": "succeeded", "summary": "report sent", "error": "",
                 "denied": [],
                 "auto_approved": [{"kind": "egress", "detail": "open.feishu.cn"},
                                   {"kind": "fs", "detail": "/DATA/reports"}]})

    await h.run(conn)
    summary = _run_row(conn, rid)["summary"]
    assert summary.startswith("report sent")
    assert "preauth used" in summary
    assert "open.feishu.cn" in summary and "/DATA/reports" in summary


@pytest.mark.asyncio
async def test_no_preauth_note_when_nothing_was_auto_approved(conn):
    tid = _mk(conn)
    rid = _queue(conn, tid)
    h = Harness()
    await h.run(conn)
    assert "preauth used" not in _run_row(conn, rid)["summary"]


# -- notify (Task 6) -----------------------------------------------------

@pytest.mark.asyncio
async def test_notify_is_called_once_per_run_with_the_committed_result(conn):
    tid = _mk(conn)
    rid = _queue(conn, tid)
    h = Harness({"status": "succeeded", "summary": "done", "error": "",
                 "denied": [], "auto_approved": []})
    calls = []

    async def fake_notify(conn_arg, task_row, run_row):
        calls.append((task_row["id"], run_row["id"], run_row["status"],
                      run_row["summary"]))
        return True

    await h.run(conn, notify=fake_notify)
    assert calls == [(tid, rid, "succeeded", "done")]


@pytest.mark.asyncio
async def test_notify_failure_does_not_affect_the_recorded_result(conn):
    """A broken channel (dead adapter, bad notify_channel, whatever) must
    never take the committed run result down with it — `finish_run` already
    landed before `notify` is even called."""
    tid = _mk(conn)
    rid = _queue(conn, tid)
    h = Harness({"status": "succeeded", "summary": "all good", "error": "",
                 "denied": [], "auto_approved": []})

    async def boom(conn_arg, task_row, run_row):
        raise RuntimeError("channel exploded")

    assert await h.run(conn, notify=boom) is True
    row = _run_row(conn, rid)
    assert row["status"] == "succeeded"
    assert row["summary"] == "all good"


@pytest.mark.asyncio
async def test_notify_skipped_when_task_was_deleted(conn):
    tid = _mk(conn)
    rid = _queue(conn, tid)
    conn.execute("DELETE FROM scheduled_tasks WHERE id=?", (tid,))  # see above
    conn.commit()
    h = Harness()
    calls = []

    async def fake_notify(conn_arg, task_row, run_row):
        calls.append(1)
        return True

    assert await h.run(conn, notify=fake_notify) is True
    assert calls == []
    assert _run_row(conn, rid)["status"] == "failed"


# -- pruning + session deletion ----------------------------------------------

def _seed_history(conn, task_id, count, snaps_root=None):
    """`count` finished runs, each owning a session that has EXCHANGED
    MESSAGES — the shape every real task run has, and the one that makes a
    naive `DELETE FROM sessions` raise (messages.session_id is a plain
    REFERENCES with no ON DELETE CASCADE, and foreign_keys is ON)."""
    out = []
    for i in range(count):
        rid = store.create_run(conn, task_id, "u1", "cron")
        sid = f"old-{i}"
        conn.execute(
            "INSERT INTO sessions (id, user_id, title, created_at, updated_at, "
            "agent_type, source) VALUES (?,?,?,?,?,?,?)",
            (sid, "u1", None, NOW, NOW, "general", "task"))
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, created_at) "
            "VALUES (?,?,?,?,?)",
            (f"msg-{i}", sid, "assistant", "[]", NOW))
        store.attach_session(conn, rid, sid)
        store.finish_run(conn, rid, "succeeded", summary="old")
        if snaps_root is not None:
            d = snaps_root / sid
            d.mkdir(parents=True, exist_ok=True)
            (d / "snap.json").write_text("{}")
        out.append((rid, sid))
    conn.commit()
    return out


def _real_deleter(monkeypatch, tmp_path):
    """process_once's production session deleter, with only the Parser call
    stubbed out — the SQL and the snapshot rmtree are the real thing."""
    snaps = tmp_path / "snapshots"
    snaps.mkdir(exist_ok=True)
    monkeypatch.setattr(runner, "_snapshots_root", lambda: str(snaps))
    calls = []

    async def no_parser(user_id, session_id):
        calls.append((user_id, session_id))

    async def deleter(conn_, user_id, session_id):
        await runner.delete_session(conn_, user_id, session_id,
                                    vector_cleanup=no_parser)

    return deleter, snaps, calls


@pytest.mark.asyncio
async def test_prune_deletes_old_sessions_for_real(conn, tmp_path, monkeypatch):
    """The regression: sessions with messages, deleted through the production
    path. A `DELETE FROM sessions` without the messages delete first raises
    IntegrityError here, which the finally-block would swallow into a warning
    — leaving every pruned session behind forever."""
    tid = _mk(conn)
    deleter, snaps, parser_calls = _real_deleter(monkeypatch, tmp_path)
    old = _seed_history(conn, tid, runner.KEEP_RUNS + 3, snaps_root=snaps)

    _queue(conn, tid)
    await Harness().run(conn, session_deleter=deleter)

    kept = conn.execute("SELECT COUNT(*) c FROM task_runs WHERE task_id=?",
                        (tid,)).fetchone()["c"]
    assert kept == runner.KEEP_RUNS

    for _, sid in old[:4]:                      # the pruned ones (53 + 1 = 54)
        assert conn.execute("SELECT 1 FROM sessions WHERE id=?",
                            (sid,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM messages WHERE session_id=?",
                            (sid,)).fetchone() is None
        assert not (snaps / sid).exists()       # snapshots dir gone
        assert ("u1", sid) in parser_calls      # vectors dropped too
    # And the surviving history is untouched.
    keep_sid = old[-1][1]
    assert conn.execute("SELECT 1 FROM sessions WHERE id=?",
                        (keep_sid,)).fetchone() is not None
    assert conn.execute("SELECT 1 FROM messages WHERE session_id=?",
                        (keep_sid,)).fetchone() is not None


@pytest.mark.asyncio
async def test_one_undeletable_session_does_not_strand_the_batch(conn, tmp_path,
                                                                 monkeypatch):
    tid = _mk(conn)
    deleter, snaps, _ = _real_deleter(monkeypatch, tmp_path)
    old = _seed_history(conn, tid, runner.KEEP_RUNS + 3, snaps_root=snaps)
    # 53 seeded runs + this tick's own run = 54, so prune drops 4: old[3]
    # down to old[0]. prune_runs returns them newest-first, so old[3] is the
    # FIRST one handled — which is the only position that can prove the try
    # sits inside the loop. Blowing up on old[0] (the last) would leave the
    # others already deleted and pass either way.
    doomed = old[3][1]

    async def flaky(conn_, user_id, session_id):
        if session_id == doomed:
            raise RuntimeError("session busy")
        await deleter(conn_, user_id, session_id)

    _queue(conn, tid)
    await Harness().run(conn, session_deleter=flaky)

    # The one that failed survives; the three handled after it were still
    # deleted rather than stranded behind it.
    assert conn.execute("SELECT 1 FROM sessions WHERE id=?",
                        (doomed,)).fetchone() is not None
    for _, sid in old[0:3]:
        assert conn.execute("SELECT 1 FROM sessions WHERE id=?",
                            (sid,)).fetchone() is None


@pytest.mark.asyncio
async def test_delete_session_survives_a_dead_parser(conn, tmp_path, monkeypatch):
    """Vector cleanup is best-effort: Parser being down must not stop the row
    deletion (same contract as main.delete_session)."""
    monkeypatch.setattr(runner, "_snapshots_root", lambda: str(tmp_path))
    tid = _mk(conn)
    sid = _seed_history(conn, tid, 1)[0][1]

    async def dead(user_id, session_id):
        raise OSError("parser unreachable")

    await runner.delete_session(conn, "u1", sid, vector_cleanup=dead)
    assert conn.execute("SELECT 1 FROM sessions WHERE id=?",
                        (sid,)).fetchone() is None


@pytest.mark.asyncio
async def test_delete_session_refuses_another_users_session(conn, tmp_path,
                                                            monkeypatch):
    monkeypatch.setattr(runner, "_snapshots_root", lambda: str(tmp_path))
    tid = _mk(conn)
    sid = _seed_history(conn, tid, 1)[0][1]

    async def noop(user_id, session_id):
        pass

    await runner.delete_session(conn, "someone-else", sid, vector_cleanup=noop)
    assert conn.execute("SELECT 1 FROM sessions WHERE id=?",
                        (sid,)).fetchone() is not None
    assert conn.execute("SELECT 1 FROM messages WHERE session_id=?",
                        (sid,)).fetchone() is not None


# -- cancellation, sink eviction --------------------------------------------

@pytest.mark.asyncio
async def test_timeout_cancels_the_still_running_agent_task(conn):
    """spec §5: the wall-clock watchdog CANCELS the run, then records timeout.
    The driver only stops reading the stream — without this the model keeps
    burning CPU and the next fire starts alongside it despite overlap=skip."""
    tid = _mk(conn)
    rid = _queue(conn, tid)
    h = Harness({"status": "timeout", "summary": "", "error": "timeout",
                 "denied": [], "auto_approved": []})

    await h.run(conn)

    assert h.cancelled == [h.sink]
    assert _run_row(conn, rid)["status"] == "timeout"


@pytest.mark.asyncio
async def test_success_does_not_cancel_anything(conn):
    tid = _mk(conn)
    _queue(conn, tid)
    h = Harness()
    await h.run(conn)
    assert h.cancelled == []


@pytest.mark.asyncio
async def test_failure_after_start_run_also_cancels(conn):
    tid = _mk(conn)
    _queue(conn, tid)
    h = Harness(driver_raises=RuntimeError("boom"))
    await h.run(conn)
    assert h.cancelled == [h.sink]


@pytest.mark.asyncio
async def test_cancel_sink_really_cancels_the_task():
    """The production `cancel_sink`, against a real asyncio task."""
    async def forever():
        await asyncio.sleep(3600)

    task = asyncio.create_task(forever())
    await asyncio.sleep(0)
    sink = FakeSink(task=task)

    assert await runner.cancel_sink(sink) is True
    assert task.cancelled()
    # A finished (or absent) task is a no-op, never a second cancel.
    assert await runner.cancel_sink(sink) is False
    assert await runner.cancel_sink(FakeSink()) is False


@pytest.mark.asyncio
async def test_cancel_grace_period_is_actually_bounded(monkeypatch):
    """A run that swallows CancelledError must not park the worker.

    asyncio.wait_for would: on its own timeout it calls _cancel_and_wait, which
    waits for the task to unwind with NO deadline. The slot's semaphore would
    then never be released — silently, since every frame here is inside a
    try/except — and the queue would stop draining forever.
    """
    monkeypatch.setattr(runner, "CANCEL_GRACE_SECONDS", 0.2)
    started, release = asyncio.Event(), asyncio.Event()

    async def stubborn():
        started.set()
        while not release.is_set():
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                pass                        # refuses to die

    task = asyncio.create_task(stubborn())
    await started.wait()
    loop = asyncio.get_running_loop()
    t0 = loop.time()

    # Waited on from the outside, so a regression fails at 2s instead of
    # hanging the suite: cancelling a wait_for that is itself stuck in
    # _cancel_and_wait would just block again.
    call = asyncio.create_task(runner.cancel_sink(FakeSink(task=task)))
    done, _ = await asyncio.wait({call}, timeout=2)
    elapsed = loop.time() - t0
    try:
        assert call in done, f"cancel_sink still blocked after {elapsed:.2f}s"
        assert call.result() is True
        assert 0.15 <= elapsed < 1.0, f"returned after {elapsed:.2f}s"
        assert not task.done()              # still running, but we moved on
    finally:
        release.set()
        call.cancel()
        task.cancel()
        await asyncio.wait({task}, timeout=1)


@pytest.mark.asyncio
async def test_finished_run_is_evicted_from_active_runs(conn):
    """main._active_runs only ever inserts (main.py). A scheduled run has no
    client that could reconnect, so its sink must be dropped or every run's
    events stay resident forever."""
    import main

    tid = _mk(conn)
    _queue(conn, tid)
    h = Harness()
    main._active_runs["sess-0"] = h.sink
    other = FakeSink()
    main._active_runs["someone-else"] = other
    try:
        await h.run(conn, evict=runner.evict_sink)
        assert "sess-0" not in main._active_runs
        assert main._active_runs["someone-else"] is other
    finally:
        main._active_runs.pop("sess-0", None)
        main._active_runs.pop("someone-else", None)


@pytest.mark.asyncio
async def test_evict_leaves_a_foreign_sink_alone():
    import main
    mine, theirs = FakeSink(), FakeSink()
    main._active_runs["sess-x"] = theirs
    try:
        runner.evict_sink("sess-x", mine)
        assert main._active_runs["sess-x"] is theirs
    finally:
        main._active_runs.pop("sess-x", None)


@pytest.mark.asyncio
async def test_zero_timeout_falls_back_instead_of_expiring_instantly(conn):
    """0 means unlimited for max_turns but would mean "already expired" for a
    deadline — the driver would return timeout on its first check."""
    tid = _mk(conn, timeout_seconds=1800)
    conn.execute("UPDATE scheduled_tasks SET timeout_seconds=0 WHERE id=?", (tid,))
    conn.commit()
    _queue(conn, tid)
    h = Harness()

    await h.run(conn)
    assert h.driver.run_timeout == runner.DEFAULT_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_prune_failure_does_not_flip_a_finished_run(conn):
    tid = _mk(conn)
    rid = _queue(conn, tid)
    h = Harness()

    def boom(*a, **kw):
        raise RuntimeError("prune exploded")

    assert await h.run(conn, prune=boom) is True
    assert _run_row(conn, rid)["status"] == "succeeded"


# -- worker wiring -----------------------------------------------------------

@pytest.mark.asyncio
async def test_start_worker_clears_orphans_then_stops(conn):
    tid = _mk(conn)
    rid = _queue(conn, tid)
    task, stop = runner.start_worker(conn)
    try:
        # requeue_orphaned_runs ran synchronously in start_worker: the queued
        # run from before the "restart" is failed, never replayed.
        assert _run_row(conn, rid)["status"] == "failed"
        await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)
    assert task.done()


@pytest.mark.asyncio
async def test_worker_loop_processes_a_queued_run(conn):
    tid = _mk(conn)
    h = Harness()
    stop = asyncio.Event()

    async def process(conn_):
        rid = _run_row_first_queued(conn_)
        if rid is None:
            return False
        return await h.run(conn_)

    loop_task = asyncio.create_task(
        runner.worker_loop(conn, stop_event=stop, process=process,
                           poll_seconds=0.01))
    rid = _queue(conn, tid)
    for _ in range(200):
        await asyncio.sleep(0.01)
        if _run_row(conn, rid)["status"] == "succeeded":
            break
    stop.set()
    await asyncio.wait_for(loop_task, timeout=2)
    assert _run_row(conn, rid)["status"] == "succeeded"


def _run_row_first_queued(conn):
    row = conn.execute(
        "SELECT id FROM task_runs WHERE status='queued' LIMIT 1").fetchone()
    return row["id"] if row else None


@pytest.mark.asyncio
async def test_worker_loop_never_exceeds_max_concurrent(conn):
    """MAX_CONCURRENT is the whole point of the semaphore: two slow runs may
    overlap, a third has to wait for a slot."""
    tid = _mk(conn)
    for _ in range(3):
        _queue(conn, tid)
    started, finished = [], []
    release = asyncio.Event()
    stop = asyncio.Event()

    async def process(conn_):
        started.append(1)
        await release.wait()
        finished.append(1)
        return True

    loop_task = asyncio.create_task(
        runner.worker_loop(conn, stop_event=stop, process=process,
                           poll_seconds=0.01))
    try:
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(started) >= runner.MAX_CONCURRENT:
                break
        await asyncio.sleep(0.1)                 # give it every chance to overrun
        assert len(started) == runner.MAX_CONCURRENT

        release.set()                            # free both slots
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(started) > runner.MAX_CONCURRENT:
                break
        assert len(started) > runner.MAX_CONCURRENT
    finally:
        release.set()
        stop.set()
        await asyncio.wait_for(loop_task, timeout=2)


@pytest.mark.asyncio
async def test_worker_loop_survives_a_raising_process(conn):
    stop = asyncio.Event()
    calls = []

    async def process(conn_):
        calls.append(1)
        raise RuntimeError("tick exploded")

    _mk(conn)
    _queue(conn, _mk(conn))
    loop_task = asyncio.create_task(
        runner.worker_loop(conn, stop_event=stop, process=process,
                           poll_seconds=0.01))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(loop_task, timeout=2)
    assert calls                                   # it ran
    assert loop_task.exception() is None           # and never died


# -- reclaiming rows the run leaves behind ------------------------------------

def _seed_agent_run(conn, session_id, run_id, events=3):
    """The rows the SSE layer writes for one agent turn: an `agent_runs` row
    and its `event_log` payloads. Neither has an FK to sessions, so neither is
    reclaimed by anything except an explicit delete."""
    conn.execute(
        "INSERT INTO agent_runs (id, session_id, user_id, status, "
        "user_message, created_at) VALUES (?,?,?,?,?,?)",
        (run_id, session_id, "u1", "done", "go", NOW))
    for seq in range(events):
        conn.execute(
            "INSERT INTO event_log (run_id, seq, payload, created_at) "
            "VALUES (?,?,?,?)", (run_id, seq, "{}", NOW))
    conn.commit()


@pytest.mark.asyncio
async def test_delete_session_reclaims_agent_runs_and_event_log(conn, tmp_path,
                                                                monkeypatch):
    """The measured leak: ~591 event_log rows per run, and the runner's own
    session delete never touched them (nor `agent_runs`, which is the only way
    back to them)."""
    monkeypatch.setattr(runner, "_snapshots_root", lambda: str(tmp_path))
    tid = _mk(conn)
    sid = _seed_history(conn, tid, 1)[0][1]
    _seed_agent_run(conn, sid, "ar-1")
    # A second session's rows must survive — the delete has to be scoped.
    conn.execute(
        "INSERT INTO sessions (id, user_id, title, created_at, updated_at, "
        "agent_type, source) VALUES (?,?,?,?,?,?,?)",
        ("keep-me", "u1", None, NOW, NOW, "general", "task"))
    _seed_agent_run(conn, "keep-me", "ar-keep")
    conn.commit()

    async def noop(user_id, session_id):
        pass

    await runner.delete_session(conn, "u1", sid, vector_cleanup=noop)

    assert conn.execute("SELECT COUNT(*) c FROM agent_runs WHERE session_id=?",
                        (sid,)).fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM event_log WHERE run_id=?",
                        ("ar-1",)).fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM agent_runs WHERE session_id=?",
                        ("keep-me",)).fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM event_log WHERE run_id=?",
                        ("ar-keep",)).fetchone()["c"] == 3


@pytest.mark.asyncio
async def test_prune_reclaims_agent_runs_and_event_log(conn, tmp_path,
                                                       monkeypatch):
    """Same thing through the production prune path, not the helper directly."""
    tid = _mk(conn)
    deleter, snaps, _ = _real_deleter(monkeypatch, tmp_path)
    old = _seed_history(conn, tid, runner.KEEP_RUNS + 3, snaps_root=snaps)
    for i, (_, sid) in enumerate(old):
        _seed_agent_run(conn, sid, f"ar-{i}")

    _queue(conn, tid)
    await Harness().run(conn, session_deleter=deleter)

    for i, (_, sid) in enumerate(old[:4]):          # the pruned ones
        assert conn.execute("SELECT COUNT(*) c FROM agent_runs WHERE session_id=?",
                            (sid,)).fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM event_log WHERE run_id=?",
                            (f"ar-{i}",)).fetchone()["c"] == 0
    kept_index = len(old) - 1
    assert conn.execute("SELECT COUNT(*) c FROM event_log WHERE run_id=?",
                        (f"ar-{kept_index}",)).fetchone()["c"] == 3


@pytest.mark.asyncio
async def test_delete_task_reclaims_every_run_and_session(conn, tmp_path,
                                                          monkeypatch):
    tid = _mk(conn)
    deleter, snaps, _ = _real_deleter(monkeypatch, tmp_path)
    old = _seed_history(conn, tid, 3, snaps_root=snaps)
    for i, (_, sid) in enumerate(old):
        _seed_agent_run(conn, sid, f"ar-{i}")
    queued = _queue(conn, tid)                      # not started yet

    assert await store.delete_task(conn, tid, "u1", session_deleter=deleter) is True

    assert conn.execute("SELECT COUNT(*) c FROM scheduled_tasks WHERE id=?",
                        (tid,)).fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM task_runs WHERE task_id=?",
                        (tid,)).fetchone()["c"] == 0
    assert _run_row(conn, queued) is None
    for i, (_, sid) in enumerate(old):
        assert conn.execute("SELECT 1 FROM sessions WHERE id=?",
                            (sid,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM messages WHERE session_id=?",
                            (sid,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM agent_runs WHERE session_id=?",
                            (sid,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM event_log WHERE run_id=?",
                            (f"ar-{i}",)).fetchone() is None
        assert not (snaps / sid).exists()


@pytest.mark.asyncio
async def test_delete_task_refuses_another_users_task(conn, tmp_path, monkeypatch):
    tid = _mk(conn)
    deleter, snaps, _ = _real_deleter(monkeypatch, tmp_path)
    old = _seed_history(conn, tid, 2, snaps_root=snaps)

    assert await store.delete_task(conn, tid, "u2", session_deleter=deleter) is False

    assert conn.execute("SELECT COUNT(*) c FROM task_runs WHERE task_id=?",
                        (tid,)).fetchone()["c"] == 2
    for _, sid in old:
        assert conn.execute("SELECT 1 FROM sessions WHERE id=?",
                            (sid,)).fetchone() is not None


@pytest.mark.asyncio
async def test_delete_task_still_removes_the_task_if_a_session_is_stuck(
        conn, tmp_path, monkeypatch):
    tid = _mk(conn)
    deleter, snaps, _ = _real_deleter(monkeypatch, tmp_path)
    old = _seed_history(conn, tid, 2, snaps_root=snaps)
    doomed = old[1][1]

    async def flaky(conn_, user_id, session_id):
        if session_id == doomed:
            raise RuntimeError("session busy")
        await deleter(conn_, user_id, session_id)

    assert await store.delete_task(conn, tid, "u1", session_deleter=flaky) is True
    assert conn.execute("SELECT COUNT(*) c FROM scheduled_tasks WHERE id=?",
                        (tid,)).fetchone()["c"] == 0
    assert conn.execute("SELECT 1 FROM sessions WHERE id=?",
                        (old[0][1],)).fetchone() is None


# -- bounded deletion (N2) ----------------------------------------------------

@pytest.mark.asyncio
async def test_delete_task_returns_within_its_budget(conn, tmp_path, monkeypatch):
    """A slow vector cleanup must not park the HTTP request.

    Each session's cleanup awaits Parser behind a 10s timeout; with 50 kept runs
    that is ~500s inside a request whose reverse proxy sets no timeout of its
    own. The delete gets a budget, and the rest moves to the background.
    """
    tid = _mk(conn)
    _seed_history(conn, tid, 8)
    fake_clock = {"t": 0.0}
    deleted = []

    async def slow(conn_, user_id, session_id):
        fake_clock["t"] += 4.0          # 4 "seconds" per session
        deleted.append(session_id)

    spawned = []

    assert await store.delete_task(
        conn, tid, "u1", session_deleter=slow, budget_seconds=10,
        monotonic=lambda: fake_clock["t"], spawn=spawned.append) is True

    # 10s of budget at 4s per session: three sessions, then the hand-off.
    assert len(deleted) == 3
    assert len(spawned) == 1
    # The task is gone for the caller, and the leftover runs are still in the
    # table WITH their sessions — a state the continuation can finish.
    assert conn.execute("SELECT COUNT(*) c FROM scheduled_tasks WHERE id=?",
                        (tid,)).fetchone()["c"] == 0
    left = conn.execute("SELECT session_id FROM task_runs WHERE task_id=?",
                        (tid,)).fetchall()
    assert len(left) == 5
    for row in left:
        assert conn.execute("SELECT 1 FROM sessions WHERE id=?",
                            (row["session_id"],)).fetchone() is not None

    # Running the continuation finishes the job, nothing lost.
    await spawned[0]
    assert conn.execute("SELECT COUNT(*) c FROM task_runs WHERE task_id=?",
                        (tid,)).fetchone()["c"] == 0
    assert len(deleted) == 8


@pytest.mark.asyncio
async def test_delete_task_within_budget_spawns_nothing(conn, tmp_path, monkeypatch):
    tid = _mk(conn)
    deleter, snaps, _ = _real_deleter(monkeypatch, tmp_path)
    _seed_history(conn, tid, 3, snaps_root=snaps)
    spawned = []

    assert await store.delete_task(conn, tid, "u1", session_deleter=deleter,
                                   spawn=spawned.append) is True
    assert spawned == []
    assert conn.execute("SELECT COUNT(*) c FROM task_runs WHERE task_id=?",
                        (tid,)).fetchone()["c"] == 0


@pytest.mark.asyncio
async def test_purge_runs_is_retryable_after_an_interruption(conn, tmp_path,
                                                             monkeypatch):
    """Interrupt the purge and no session is left unreferenced: the runs that
    remain still point at theirs, so re-running finishes the job. The old shape
    (delete every run row, then walk the ids from memory) lost them."""
    tid = _mk(conn)
    deleter, snaps, _ = _real_deleter(monkeypatch, tmp_path)
    old = _seed_history(conn, tid, 4, snaps_root=snaps)

    boom = old[2][1]

    async def interrupted(conn_, user_id, session_id):
        if session_id == boom:
            raise asyncio.CancelledError()
        await deleter(conn_, user_id, session_id)

    with pytest.raises(asyncio.CancelledError):
        await store.purge_runs(conn, tid, "u1", session_deleter=interrupted)

    # Every surviving run row still names a session that still exists.
    rows = conn.execute("SELECT session_id FROM task_runs WHERE task_id=?",
                        (tid,)).fetchall()
    assert rows
    for row in rows:
        assert conn.execute("SELECT 1 FROM sessions WHERE id=?",
                            (row["session_id"],)).fetchone() is not None
    # And nothing is orphaned: every session that still exists is referenced.
    referenced = {r["session_id"] for r in rows}
    for _, sid in old:
        exists = conn.execute("SELECT 1 FROM sessions WHERE id=?",
                              (sid,)).fetchone() is not None
        assert exists == (sid in referenced)

    # Retry completes it.
    assert await store.purge_runs(conn, tid, "u1", session_deleter=deleter) is True
    assert conn.execute("SELECT COUNT(*) c FROM task_runs WHERE task_id=?",
                        (tid,)).fetchone()["c"] == 0


@pytest.mark.asyncio
async def test_purge_runs_does_not_spin_on_an_undeletable_session(conn, tmp_path,
                                                                 monkeypatch):
    tid = _mk(conn)
    deleter, snaps, _ = _real_deleter(monkeypatch, tmp_path)
    old = _seed_history(conn, tid, 3, snaps_root=snaps)
    doomed = old[0][1]

    async def flaky(conn_, user_id, session_id):
        if session_id == doomed:
            raise RuntimeError("session busy")
        await deleter(conn_, user_id, session_id)

    assert await store.purge_runs(conn, tid, "u1", session_deleter=flaky) is True
    assert conn.execute("SELECT COUNT(*) c FROM task_runs WHERE task_id=?",
                        (tid,)).fetchone()["c"] == 0


@pytest.mark.asyncio
async def test_reclaim_orphaned_runs_finishes_an_abandoned_delete(conn, tmp_path,
                                                                  monkeypatch):
    """The durability net: the process died after the task row went, leaving
    runs nothing else can find (every other path walks by task_id)."""
    tid = _mk(conn)
    deleter, snaps, _ = _real_deleter(monkeypatch, tmp_path)
    old = _seed_history(conn, tid, 3, snaps_root=snaps)
    for i, (_, sid) in enumerate(old):
        _seed_agent_run(conn, sid, f"ar-{i}")
    # A second, live task whose run must survive the reclaim untouched.
    # (Seeded by hand: _seed_history names its sessions old-<i>, which would
    # collide with the ones above.)
    live = _mk(conn, name="still here")
    live_sid = "live-session"
    conn.execute(
        "INSERT INTO sessions (id, user_id, title, created_at, updated_at, "
        "agent_type, source) VALUES (?,?,?,?,?,?,?)",
        (live_sid, "u1", None, NOW, NOW, "general", "task"))
    live_run = store.create_run(conn, live, "u1", "cron")
    store.attach_session(conn, live_run, live_sid)
    store.finish_run(conn, live_run, "succeeded", summary="live")
    conn.commit()

    conn.execute("DELETE FROM scheduled_tasks WHERE id=?", (tid,))
    conn.commit()

    assert await store.reclaim_orphaned_runs(conn, session_deleter=deleter) == 3

    for i, (_, sid) in enumerate(old):
        assert conn.execute("SELECT 1 FROM sessions WHERE id=?",
                            (sid,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM event_log WHERE run_id=?",
                            (f"ar-{i}",)).fetchone() is None
    assert conn.execute("SELECT COUNT(*) c FROM task_runs WHERE task_id=?",
                        (live,)).fetchone()["c"] == 1
    assert conn.execute("SELECT 1 FROM sessions WHERE id=?",
                        (live_sid,)).fetchone() is not None
    # Idempotent.
    assert await store.reclaim_orphaned_runs(conn, session_deleter=deleter) == 0


@pytest.mark.asyncio
async def test_purge_runs_caps_one_hanging_session_at_the_budget(conn, tmp_path,
                                                                monkeypatch):
    """A single session whose vector cleanup hangs must not push the delete
    past its deadline — Parser's own timeout is 10s, which would double a 10s
    budget on its own. The run row survives, so the continuation retries it."""
    tid = _mk(conn)
    old = _seed_history(conn, tid, 3)

    async def hangs(conn_, user_id, session_id):
        await asyncio.sleep(30)

    started = asyncio.get_running_loop().time()
    loop = asyncio.get_running_loop()
    done = await store.purge_runs(conn, tid, "u1", session_deleter=hangs,
                                  deadline=loop.time() + 0.2,
                                  monotonic=loop.time)
    elapsed = asyncio.get_running_loop().time() - started

    assert done is False
    assert elapsed < 2.0, f"purge took {elapsed:.2f}s despite a 0.2s budget"
    # Nothing lost: the run rows are all still there with their sessions.
    assert conn.execute("SELECT COUNT(*) c FROM task_runs WHERE task_id=?",
                        (tid,)).fetchone()["c"] == 3
    for _, sid in old:
        assert conn.execute("SELECT 1 FROM sessions WHERE id=?",
                            (sid,)).fetchone() is not None


# -- start notification ------------------------------------------------------

@pytest.mark.asyncio
async def test_start_notification_fires_before_the_agent_does(conn):
    """The whole point is telling the user work BEGAN, so it cannot wait for
    the run to finish — it must be sent before start_run hands the prompt to
    the agent."""
    h = Harness()
    task_id = _mk(conn)
    _queue(conn, task_id)
    order = []

    async def notify_start(c, t, r):
        order.append("start-notify")
        return True

    real_start_run = h.start_run

    def start_run(*a, **kw):
        order.append("start-run")
        return real_start_run(*a, **kw)

    await h.run(conn, notify_start=notify_start, start_run=start_run)
    assert order == ["start-notify", "start-run"]


@pytest.mark.asyncio
async def test_no_start_notification_when_the_run_never_starts(conn):
    """A task with no model never reaches the agent; announcing a start the
    user then never gets a result for is worse than saying nothing."""
    h = Harness()
    task_id = _mk(conn, model="")
    _queue(conn, task_id)
    called = []

    async def notify_start(c, t, r):
        called.append(1)
        return True

    with mock.patch.object(runner.notes_store, "get_background_model",
                           return_value=""):
        await h.run(conn, notify_start=notify_start)
    assert called == []


@pytest.mark.asyncio
async def test_a_broken_start_notification_does_not_stop_the_run(conn):
    h = Harness()
    task_id = _mk(conn)
    _queue(conn, task_id)

    async def notify_start(c, t, r):
        raise RuntimeError("channel down")

    await h.run(conn, notify_start=notify_start)
    row = conn.execute("SELECT status FROM task_runs WHERE task_id=?",
                       (task_id,)).fetchone()
    assert row["status"] == "succeeded"


@pytest.mark.asyncio
async def test_the_task_workspace_is_granted_and_briefed(conn):
    """A task's own folder reaches BOTH gates and the model's prompt.

    Three things have to line up or the folder is useless: `grant_fs` has to
    receive it (so the fs skill can see it), the driver's preauth document has
    to contain it (so a write card is auto-approved), and the prompt has to name
    it (so the model knows it exists at all).
    """
    h = Harness()
    h.workspace_path = "/tmp/ws/t-1"
    tid = _mk(conn)
    rid = _queue(conn, tid)

    granted = []
    h.grant_fs = lambda conn_, sid, paths: granted.append(list(paths)) or len(paths)

    assert await h.run(conn) is True

    assert h.workspaces and h.workspaces[0][0] == tid
    assert "/tmp/ws/t-1" in granted[0]
    assert "/tmp/ws/t-1" in h.driver.preauth["fs_write"]
    assert "/tmp/ws/t-1" in h.start_run_calls[0]["message"]
    assert rid


@pytest.mark.asyncio
async def test_a_run_without_a_workspace_keeps_the_prompt_verbatim(conn):
    # The folder can fail to be created (a full disk, a bad root). That costs
    # the folder, never the run — and the prompt must be untouched.
    h = Harness()
    h.workspace_path = ""
    tid = _mk(conn, prompt="write the report")
    _queue(conn, tid)

    assert await h.run(conn) is True
    assert h.start_run_calls[0]["message"] == "write the report"


@pytest.mark.asyncio
async def test_the_file_tools_are_unlocked_before_the_run_starts(conn):
    """Ordering, not just presence.

    `agent.py` seeds UNLOCKED_VAR from the session row at the top of the run, so
    an unlock written after start_run is read too late and the tool stays
    invisible — the exact symptom this feature had before the fix, with nothing
    in the logs to explain it.
    """
    h = Harness()
    h.workspace_path = "/tmp/ws/t-1"
    order = []
    unlocked = {}

    def read_unlocked(session_id):
        return ["web"]

    def unlock(session_id, categories):
        order.append("unlock")
        unlocked[session_id] = list(categories)

    real_start = h.start_run

    def start_run(*a, **kw):
        order.append("start_run")
        return real_start(*a, **kw)

    tid = _mk(conn)
    _queue(conn, tid)
    assert await h.run(conn, read_unlocked=read_unlocked, unlock=unlock,
                       start_run=start_run) is True

    assert order == ["unlock", "start_run"], order
    assert set(next(iter(unlocked.values()))) == {"web", "files"}


@pytest.mark.asyncio
async def test_a_task_without_a_workspace_does_not_touch_the_tool_gate(conn):
    # This feature must not quietly widen the tool surface for every task.
    h = Harness()
    h.workspace_path = ""
    calls = []
    tid = _mk(conn)
    _queue(conn, tid)

    assert await h.run(conn, unlock=lambda s, c: calls.append(c)) is True
    assert calls == []


@pytest.mark.asyncio
async def test_driver_factory_receives_task_row(conn):
    # The escalation coordinator (tasks/escalate.py) needs the task row —
    # notify_channel, id, user_id — so the factory contract carries it.
    h = Harness()
    task_id = _mk(conn)
    _queue(conn, task_id)
    assert await h.run(conn) is True
    assert h.driver.task is not None
    assert h.driver.task["id"] == task_id
    assert h.driver.task["user_id"] == "u1"
