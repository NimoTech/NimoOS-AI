"""Scheduler tick — turning due scheduled_tasks into queued task_runs.

`tick_once` is a pure-ish function over the connection plus an injected
`now`, so every policy branch (overlap / catchup / broken expression) is
testable without a clock or an event loop.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

import db as db_module
from tasks import scheduler, store

NOW = 1_800_000_000  # fixed "now" for every test


@pytest.fixture
def conn(tmp_path):
    return db_module.init_db(str(tmp_path / "t.db"))


def _mk(conn, **over):
    kw = dict(name="daily", prompt="do it", trigger_type="cron",
              cron_expr="*/5 * * * *", interval_seconds=0, agent_type="general",
              model="", max_turns=25, timeout_seconds=1800,
              overlap_policy="skip", catchup_policy="skip",
              preauth={}, notify_policy="failure", notify_channel="")
    kw.update(over)
    return store.create_task(conn, "u1", **kw)


def _runs(conn, task_id):
    return store.list_runs(conn, task_id)


def _task(conn, task_id):
    return store.get_task(conn, task_id, "u1")


# -- happy path --------------------------------------------------------------

def test_due_task_is_enqueued_and_next_run_advances(conn):
    tid = _mk(conn)
    store.set_next_run(conn, tid, NOW - 5)          # just fired, not "missed"

    assert scheduler.tick_once(conn, now=NOW) == 1

    runs = _runs(conn, tid)
    assert len(runs) == 1
    assert runs[0]["status"] == "queued"
    assert runs[0]["trigger"] == "cron"
    row = _task(conn, tid)
    assert row["next_run_at"] > NOW                 # strictly in the future
    assert row["last_run_at"] == NOW


def test_not_due_yet_is_left_alone(conn):
    tid = _mk(conn)
    store.set_next_run(conn, tid, NOW + 60)
    assert scheduler.tick_once(conn, now=NOW) == 0
    assert _runs(conn, tid) == []


def test_interval_task_advances_by_its_interval(conn):
    tid = _mk(conn, trigger_type="interval", cron_expr="",
              interval_seconds=900)
    store.set_next_run(conn, tid, NOW - 5)

    assert scheduler.tick_once(conn, now=NOW) == 1
    assert _runs(conn, tid)[0]["trigger"] == "interval"
    assert _task(conn, tid)["next_run_at"] == NOW + 900


def test_webhook_only_is_never_scheduled(conn):
    tid = _mk(conn, trigger_type="webhook_only", cron_expr="")
    # next_run_at is 0 for webhook_only; even a "past" now must not pick it up.
    assert _task(conn, tid)["next_run_at"] == 0
    assert scheduler.tick_once(conn, now=NOW) == 0
    assert _runs(conn, tid) == []


def test_disabled_task_is_never_scheduled(conn):
    tid = _mk(conn)
    store.set_next_run(conn, tid, NOW - 5)
    conn.execute("UPDATE scheduled_tasks SET enabled=0 WHERE id=?", (tid,))
    conn.commit()
    assert scheduler.tick_once(conn, now=NOW) == 0


# -- overlap policy ----------------------------------------------------------

@pytest.mark.parametrize("active_status", ["queued", "running"])
def test_overlap_skip_records_a_skipped_run(conn, active_status):
    tid = _mk(conn, overlap_policy="skip")
    old = store.create_run(conn, tid, "u1", "cron")
    if active_status == "running":
        store.claim_run(conn)
    store.set_next_run(conn, tid, NOW - 5)

    assert scheduler.tick_once(conn, now=NOW) == 0

    runs = _runs(conn, tid)
    assert len(runs) == 2
    skipped = [r for r in runs if r["id"] != old]
    assert len(skipped) == 1 and skipped[0]["status"] == "skipped"
    assert skipped[0]["error"]                      # says why
    # Schedule still moves on, otherwise the task would re-fire every tick.
    assert _task(conn, tid)["next_run_at"] > NOW


def test_overlap_queue_enqueues_anyway(conn):
    tid = _mk(conn, overlap_policy="queue")
    store.create_run(conn, tid, "u1", "cron")
    store.set_next_run(conn, tid, NOW - 5)

    assert scheduler.tick_once(conn, now=NOW) == 1
    assert [r["status"] for r in _runs(conn, tid)] == ["queued", "queued"]


def test_overlap_skip_ignores_finished_runs(conn):
    tid = _mk(conn, overlap_policy="skip")
    rid = store.create_run(conn, tid, "u1", "cron")
    store.claim_run(conn)
    store.finish_run(conn, rid, "succeeded", summary="ok")
    store.set_next_run(conn, tid, NOW - 5)

    assert scheduler.tick_once(conn, now=NOW) == 1


# -- catchup policy ----------------------------------------------------------

MISSED_AT = NOW - 3600  # far older than TICK_SECONDS*4 => agent was down


def test_catchup_skip_only_advances_the_schedule(conn):
    tid = _mk(conn, catchup_policy="skip")
    store.set_next_run(conn, tid, MISSED_AT)

    assert scheduler.tick_once(conn, now=NOW) == 0
    assert _runs(conn, tid) == []
    assert _task(conn, tid)["next_run_at"] > NOW


def test_catchup_run_once_enqueues_exactly_one(conn):
    tid = _mk(conn, catchup_policy="run_once")
    store.set_next_run(conn, tid, MISSED_AT)

    assert scheduler.tick_once(conn, now=NOW) == 1
    assert len(_runs(conn, tid)) == 1
    # And the next tick must NOT replay it again.
    assert scheduler.tick_once(conn, now=NOW) == 0
    assert len(_runs(conn, tid)) == 1


# -- broken configuration ----------------------------------------------------

def test_bad_cron_disables_the_task_and_records_a_failure(conn):
    tid = _mk(conn)
    conn.execute("UPDATE scheduled_tasks SET cron_expr=? WHERE id=?",
                 ("not a cron", tid))
    conn.commit()
    store.set_next_run(conn, tid, NOW - 5)

    assert scheduler.tick_once(conn, now=NOW) == 0

    row = _task(conn, tid)
    assert row["enabled"] == 0
    assert row["next_run_at"] == 0
    runs = _runs(conn, tid)
    assert len(runs) == 1 and runs[0]["status"] == "failed"
    assert "cron" in runs[0]["error"].lower()
    # A second tick must not see it any more (it is disabled).
    assert scheduler.tick_once(conn, now=NOW) == 0
    assert len(_runs(conn, tid)) == 1


def test_interval_without_a_positive_period_is_disabled(conn):
    tid = _mk(conn, trigger_type="interval", cron_expr="", interval_seconds=60)
    conn.execute("UPDATE scheduled_tasks SET interval_seconds=0 WHERE id=?",
                 (tid,))
    conn.commit()
    store.set_next_run(conn, tid, NOW - 5)

    assert scheduler.tick_once(conn, now=NOW) == 0
    assert _task(conn, tid)["enabled"] == 0
    assert _runs(conn, tid)[0]["status"] == "failed"


def test_one_broken_task_does_not_block_the_others(conn):
    bad = _mk(conn, name="bad")
    conn.execute("UPDATE scheduled_tasks SET cron_expr=? WHERE id=?",
                 ("nonsense", bad))
    conn.commit()
    store.set_next_run(conn, bad, NOW - 10)
    good = _mk(conn, name="good")
    store.set_next_run(conn, good, NOW - 5)

    assert scheduler.tick_once(conn, now=NOW) == 1
    assert len(_runs(conn, good)) == 1


# -- notifications for runs that never reach the runner ----------------------
#
# `_history` writes a terminal run row directly, bypassing tasks/runner.py's
# `finally` block — the single call point for notifications. Without an
# explicit call here, a user with notify_policy=always is never told that a
# run was skipped or that their task disabled itself.

@pytest.mark.asyncio
async def test_overlap_skip_notifies(conn):
    import asyncio
    sent = []

    async def fake_notify(c, task_row, run_row):
        sent.append((task_row["id"], run_row["status"]))
        return True

    tid = _mk(conn, overlap_policy="skip", notify_policy="always")
    store.create_run(conn, tid, "u1", "cron")
    store.set_next_run(conn, tid, NOW - 5)

    assert scheduler.tick_once(conn, now=NOW, notify=fake_notify) == 0
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert sent == [(tid, "skipped")]


@pytest.mark.asyncio
async def test_self_disable_notifies(conn):
    import asyncio
    sent = []

    async def fake_notify(c, task_row, run_row):
        sent.append((task_row["id"], run_row["status"], run_row["error"]))
        return True

    tid = _mk(conn, notify_policy="always")
    conn.execute("UPDATE scheduled_tasks SET cron_expr=? WHERE id=?",
                 ("nonsense", tid))
    conn.commit()
    store.set_next_run(conn, tid, NOW - 5)

    assert scheduler.tick_once(conn, now=NOW, notify=fake_notify) == 0
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(sent) == 1
    assert sent[0][1] == "failed" and "cron" in sent[0][2].lower()


@pytest.mark.asyncio
async def test_notify_failure_never_breaks_the_tick(conn):
    import asyncio

    async def boom(c, task_row, run_row):
        raise RuntimeError("channel down")

    tid = _mk(conn, overlap_policy="skip")
    store.create_run(conn, tid, "u1", "cron")
    store.set_next_run(conn, tid, NOW - 5)

    assert scheduler.tick_once(conn, now=NOW, notify=boom) == 0
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # the skipped row and the advanced schedule are still correct
    assert [r["status"] for r in _runs(conn, tid)] == ["skipped", "queued"]
    assert _task(conn, tid)["next_run_at"] > NOW


def test_tick_outside_an_event_loop_still_works(conn):
    """No running loop (a sync caller) — the tick must not raise; the
    notification is simply dropped."""
    tid = _mk(conn, overlap_policy="skip")
    store.create_run(conn, tid, "u1", "cron")
    store.set_next_run(conn, tid, NOW - 5)
    assert scheduler.tick_once(conn, now=NOW) == 0
    assert len(_runs(conn, tid)) == 2


# -- worker wiring -----------------------------------------------------------

@pytest.mark.asyncio
async def test_start_worker_ticks_and_stops(conn):
    import asyncio
    tid = _mk(conn)
    store.set_next_run(conn, tid, 1)  # long past, catchup default skip
    task, stop = scheduler.start_worker(conn)
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    # The tick ran: next_run_at was pushed into the future.
    assert _task(conn, tid)["next_run_at"] > 1
