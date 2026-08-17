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
    """Stand-in for main.RunSink; the fake driver never reads it."""


class FakeDriver:
    def __init__(self, result, *, session_id="", preauth=None, run_timeout=0,
                 raises=None):
        self.result = result
        self.session_id = session_id
        self.preauth = preauth
        self.run_timeout = run_timeout
        self.raises = raises
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

    async def run(self, conn, **over):
        kw = dict(start_run=self.start_run, creds_resolver=self.creds_resolver,
                  driver_factory=self.driver_factory,
                  session_factory=self.session_factory,
                  grant_fs=self.grant_fs, grant_egress=self.grant_egress,
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
    store.delete_task(conn, tid, "u1")
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


# -- pruning -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_prune_drops_old_runs_and_their_sessions(conn):
    tid = _mk(conn)
    old_ids = []
    for i in range(runner.KEEP_RUNS + 3):
        rid = store.create_run(conn, tid, "u1", "cron")
        sid = f"old-{i}"
        conn.execute(
            "INSERT INTO sessions (id, user_id, title, created_at, updated_at, "
            "agent_type, source) VALUES (?,?,?,?,?,?,?)",
            (sid, "u1", None, NOW, NOW, "general", "task"))
        store.attach_session(conn, rid, sid)
        store.finish_run(conn, rid, "succeeded", summary="old")
        old_ids.append((rid, sid))
    conn.commit()

    _queue(conn, tid)
    await Harness().run(conn)

    kept = conn.execute("SELECT COUNT(*) c FROM task_runs WHERE task_id=?",
                        (tid,)).fetchone()["c"]
    assert kept == runner.KEEP_RUNS
    # The oldest runs' sessions are gone, the newest ones are still there.
    gone = [sid for _, sid in old_ids[:3]]
    for sid in gone:
        assert conn.execute("SELECT 1 FROM sessions WHERE id=?",
                            (sid,)).fetchone() is None
    assert conn.execute("SELECT 1 FROM sessions WHERE id=?",
                        (old_ids[-1][1],)).fetchone() is not None


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
