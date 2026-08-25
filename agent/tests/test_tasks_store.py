import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import pytest
import db as db_module


@pytest.fixture
def conn(tmp_path):
    return db_module.init_db(str(tmp_path / "t.db"))


def _mk(conn, **over):
    from tasks import store
    kw = dict(name="daily", prompt="do it", trigger_type="cron",
              cron_expr="0 9 * * *", interval_seconds=0, agent_type="general",
              model="", max_turns=25, timeout_seconds=1800,
              overlap_policy="skip", catchup_policy="skip",
              preauth={}, notify_policy="failure", notify_channel="")
    kw.update(over)
    return store.create_task(conn, "u1", **kw)


def test_create_and_get_roundtrip(conn):
    from tasks import store
    tid = _mk(conn)
    row = store.get_task(conn, tid, "u1")
    assert row["name"] == "daily" and row["enabled"] == 1
    assert row["webhook_token"] and len(row["webhook_token"]) >= 32
    assert row["next_run_at"] > 0  # 建任务即算出下次触发


def test_get_task_is_user_scoped(conn):
    from tasks import store
    tid = _mk(conn)
    assert store.get_task(conn, tid, "someone-else") is None


def test_due_tasks_only_enabled_and_past(conn):
    from tasks import store
    tid = _mk(conn)
    store.set_next_run(conn, tid, 100)
    assert [r["id"] for r in store.due_tasks(conn, 200)] == [tid]
    assert store.due_tasks(conn, 50) == []
    store.update_task(conn, tid, "u1", enabled=0)
    assert store.due_tasks(conn, 200) == []


def test_run_lifecycle_and_claim_is_atomic(conn):
    from tasks import store
    tid = _mk(conn)
    rid = store.create_run(conn, tid, "u1", "cron")
    claimed = store.claim_run(conn)
    assert claimed["id"] == rid and claimed["status"] == "running"
    assert store.claim_run(conn) is None  # 已无 queued
    store.finish_run(conn, rid, "succeeded", summary="ok")
    rows = store.list_runs(conn, tid)
    assert rows[0]["status"] == "succeeded" and rows[0]["summary"] == "ok"


def test_orphan_runs_marked_failed_never_requeued(conn):
    from tasks import store
    tid = _mk(conn)
    rid = store.create_run(conn, tid, "u1", "cron")
    store.claim_run(conn)
    n = store.requeue_orphaned_runs(conn)
    assert n == 1
    row = store.list_runs(conn, tid)[0]
    assert row["status"] == "failed" and "restart" in (row["error"] or "").lower()
    assert store.claim_run(conn) is None  # 绝不重跑


def test_denied_actions_roundtrip(conn):
    from tasks import store
    tid = _mk(conn)
    rid = store.create_run(conn, tid, "u1", "manual")
    store.claim_run(conn)
    store.finish_run(conn, rid, "failed", error="boom",
                     denied=[{"kind": "shell", "detail": "rm -rf /"}])
    row = store.list_runs(conn, tid)[0]
    import json
    assert json.loads(row["denied_actions"])[0]["kind"] == "shell"


def test_prune_runs_returns_session_ids(conn):
    from tasks import store
    tid = _mk(conn)
    ids = []
    for i in range(5):
        rid = store.create_run(conn, tid, "u1", "cron")
        store.attach_session(conn, rid, f"sess-{i}")
        ids.append(rid)
    dropped = store.prune_runs(conn, tid, keep=2)
    assert len(dropped) == 3 and "sess-0" in dropped
    assert len(store.list_runs(conn, tid)) == 2


def test_continue_run_row_shape(conn):
    from tasks import store
    tid = _mk(conn)
    parent = store.create_run(conn, tid, "u1", "cron")
    store.attach_session(conn, parent, "sess-p")
    rid = store.create_continue_run(conn, tid, "u1", session_id="sess-p",
                                    resumed_from=parent,
                                    resume_message="continue please")
    row = conn.execute("SELECT * FROM task_runs WHERE id=?", (rid,)).fetchone()
    assert row["trigger"] == "manual" and row["status"] == "queued"
    assert row["session_id"] == "sess-p"          # 入队即预挂父会话
    assert row["resumed_from"] == parent
    assert row["resume_message"] == "continue please"


def test_session_active_run_detects_queued_and_running(conn):
    from tasks import store
    tid = _mk(conn)
    parent = store.create_run(conn, tid, "u1", "cron")
    store.attach_session(conn, parent, "sess-p")
    assert store.session_active_run(conn, "sess-p") is not None  # queued
    store.claim_run(conn)
    assert store.session_active_run(conn, "sess-p") is not None  # running
    store.finish_run(conn, parent, "failed", error="x")
    assert store.session_active_run(conn, "sess-p") is None
    assert store.session_active_run(conn, "") is None


def test_prune_keeps_session_shared_with_surviving_run(conn):
    from tasks import store
    tid = _mk(conn)
    parent = store.create_run(conn, tid, "u1", "cron")
    store.attach_session(conn, parent, "sess-shared")
    cont = store.create_continue_run(conn, tid, "u1", session_id="sess-shared",
                                     resumed_from=parent, resume_message="go")
    # keep=1 drops the parent (oldest) but the continuation still points at
    # the shared session — it must NOT be handed back for deletion.
    dropped = store.prune_runs(conn, tid, keep=1)
    assert dropped == []
    assert [r["id"] for r in store.list_runs(conn, tid)] == [cont]
    # Once the last referencing run is dropped, the session is released once.
    for _ in range(2):
        store.create_run(conn, tid, "u1", "cron")
    dropped = store.prune_runs(conn, tid, keep=1)
    assert dropped == ["sess-shared"]


def test_prune_dedups_shared_session_when_both_rows_drop(conn):
    from tasks import store
    tid = _mk(conn)
    parent = store.create_run(conn, tid, "u1", "cron")
    store.attach_session(conn, parent, "sess-shared")
    store.create_continue_run(conn, tid, "u1", session_id="sess-shared",
                              resumed_from=parent, resume_message="go")
    newer = store.create_run(conn, tid, "u1", "cron")
    store.attach_session(conn, newer, "sess-new")
    dropped = store.prune_runs(conn, tid, keep=1)
    assert dropped == ["sess-shared"]  # 两行共享,只交还一次


def test_prompt_revision_history_and_cap(conn):
    from tasks import store
    tid = _mk(conn)
    for i in range(store.PROMPT_REVISIONS_KEEP + 5):
        store.add_prompt_revision(conn, tid, "u1", f"v{i}", "agent", f"r{i}")
    conn.commit()
    rows = store.list_prompt_revisions(conn, tid, "u1")
    assert len(rows) == store.PROMPT_REVISIONS_KEEP
    assert rows[0]["prompt"] == f"v{store.PROMPT_REVISIONS_KEEP + 4}"  # newest first
    assert rows[0]["revised_by"] == "agent"
    assert store.list_prompt_revisions(conn, tid, "someone-else") == []


def test_user_prompt_edit_joins_the_history(conn):
    from tasks import store
    tid = _mk(conn)
    store.update_task(conn, tid, "u1", prompt="do it v2")
    store.update_task(conn, tid, "u1", prompt="do it v2")   # unchanged: no row
    store.update_task(conn, tid, "u1", name="renamed")      # no prompt: no row
    rows = store.list_prompt_revisions(conn, tid, "u1")
    assert len(rows) == 1
    assert rows[0]["prompt"] == "do it" and rows[0]["revised_by"] == "user"


def test_delete_task_clears_prompt_revisions(conn):
    import asyncio
    from tasks import store
    tid = _mk(conn)
    store.add_prompt_revision(conn, tid, "u1", "old", "agent")
    conn.commit()

    async def deleter(conn_, user, sid):
        pass
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        store.delete_task(conn, tid, "u1", session_deleter=deleter))
    assert conn.execute("SELECT COUNT(*) FROM task_prompt_revisions "
                        "WHERE task_id=?", (tid,)).fetchone()[0] == 0


def test_backfill_migrates_prev_prompt_once(conn, tmp_path):
    # A pre-history DB: prev_prompt set, no revision rows. Re-running init_db
    # (every boot does) must backfill exactly one row, exactly once.
    from tasks import store
    tid = _mk(conn)
    conn.execute("UPDATE scheduled_tasks SET prev_prompt='ancient', "
                 "prompt_revised_at=111, prompt_revised_by='agent' WHERE id=?",
                 (tid,))
    conn.execute("DELETE FROM task_prompt_revisions")
    conn.commit()
    for _ in range(2):
        db_module.init_db(str(tmp_path / "t.db"))
    rows = store.list_prompt_revisions(conn, tid, "u1")
    assert len(rows) == 1
    assert rows[0]["prompt"] == "ancient" and rows[0]["revised_by"] == "agent"
    assert rows[0]["created_at"] == 111


def test_user_prompt_edit_clears_agent_revision(conn):
    from tasks import store
    tid = _mk(conn)
    conn.execute("UPDATE scheduled_tasks SET prev_prompt='old', "
                 "prompt_revised_at=123, prompt_revised_by='agent' WHERE id=?",
                 (tid,))
    conn.commit()
    # Saving without changing the prompt keeps the revision badge alive.
    store.update_task(conn, tid, "u1", prompt="do it", name="renamed")
    row = store.get_task(conn, tid, "u1")
    assert row["prompt_revised_by"] == "agent" and row["prev_prompt"] == "old"
    # An actual prompt change supersedes the revision.
    store.update_task(conn, tid, "u1", prompt="do it differently")
    row = store.get_task(conn, tid, "u1")
    assert row["prev_prompt"] == "" and row["prompt_revised_at"] == 0
    assert row["prompt_revised_by"] == ""


def test_create_task_enabled_override(conn):
    from tasks import store
    tid = _mk(conn, trigger_type="webhook_only", cron_expr="", enabled=0)
    assert store.get_task(conn, tid, "u1")["enabled"] == 0


def test_create_task_enabled_defaults_to_1(conn):
    from tasks import store
    tid = _mk(conn, trigger_type="webhook_only", cron_expr="")
    assert store.get_task(conn, tid, "u1")["enabled"] == 1
