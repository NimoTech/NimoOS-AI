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


def test_create_task_enabled_override(conn):
    from tasks import store
    tid = _mk(conn, trigger_type="webhook_only", cron_expr="", enabled=0)
    assert store.get_task(conn, tid, "u1")["enabled"] == 0


def test_create_task_enabled_defaults_to_1(conn):
    from tasks import store
    tid = _mk(conn, trigger_type="webhook_only", cron_expr="")
    assert store.get_task(conn, tid, "u1")["enabled"] == 1
