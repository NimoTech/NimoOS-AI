"""Scheduler tick — turn due `scheduled_tasks` rows into queued `task_runs`.

`tick_once` is a pure-ish function over (conn, now): every policy branch
(overlap / catchup / broken expression) is decided from stored state plus the
injected clock, so all of them are testable without a real clock or an event
loop. `start_worker` is the thin timer around it, shaped exactly like
`notes_distill.start_worker` (stop_event + `wait_for` instead of `sleep`, so
shutdown is immediate).

Two rules this module exists to enforce:

* **The schedule always moves forward.** Every branch that decides *not* to
  enqueue still writes a future `next_run_at`. Leaving a past `next_run_at`
  behind would make `due_tasks` return the same row on every 15s tick forever
  (a skipped overlap would turn into a busy loop that also writes a `skipped`
  history row 4 times a minute).
* **A broken task disables itself.** A cron expression the user edited into
  nonsense — or an interval task whose period was zeroed — can never fire, so
  the task is disabled with one `failed` history row explaining why. The
  alternative (raise and retry) is the wiki-summary-worker failure class: an
  unfixable row re-attempted forever.
"""
from __future__ import annotations

import asyncio
import logging
import time

from . import cron, store

logger = logging.getLogger("nimoos-agent.tasks")

TICK_SECONDS = 15

# A `next_run_at` older than this is treated as "the agent was down when this
# should have fired" rather than "the tick is running a couple of seconds
# late", which is what `catchup_policy` arbitrates. 4 ticks (60s) is loose
# enough that ordinary tick jitter, a slow DB or a paused container never
# counts as a missed fire.
MISSED_AFTER_SECONDS = TICK_SECONDS * 4

# task_runs.trigger has a CHECK constraint (cron/interval/webhook/manual) that
# does NOT include 'webhook_only'. A webhook_only task should never be due
# (store.due_tasks filters next_run_at>0 and webhook_only keeps 0), but if one
# ever is — someone hand-edited the row — inserting its trigger_type verbatim
# would raise IntegrityError instead of recording anything.
_SCHEDULABLE_TRIGGERS = ("cron", "interval")


def _trigger_of(task) -> str:
    return task["trigger_type"] if task["trigger_type"] in _SCHEDULABLE_TRIGGERS else "manual"


def _next_fire(task, now: int) -> int:
    """Next firing time strictly after `now`, or raise.

    Computed from `now`, never from the stored `next_run_at`: after a missed
    fire the stored value can be hours old, and advancing from it would return
    another past timestamp — the task would then re-fire on every tick until
    the schedule caught up with the wall clock (a `*/5` cron down for a day
    would replay 288 times). Raises `cron.CronError` / `ValueError` for a
    configuration that can never fire; the caller disables the task.
    """
    trigger = task["trigger_type"]
    if trigger == "cron":
        return cron.next_after(task["cron_expr"], now)
    if trigger == "interval":
        period = int(task["interval_seconds"] or 0)
        if period <= 0:
            raise ValueError("interval_seconds must be positive")
        return now + period
    raise ValueError(f"trigger_type {trigger!r} is not schedulable")


def _history(conn, task, status: str, error: str) -> None:
    """Record a terminal run row that never ran (skipped / failed)."""
    run_id = store.create_run(conn, task["id"], task["user_id"], _trigger_of(task))
    store.finish_run(conn, run_id, status, error=error)


def _disable(conn, task, reason: str) -> None:
    conn.execute(
        "UPDATE scheduled_tasks SET enabled=0, next_run_at=0, updated_at=? WHERE id=?",
        (int(time.time()), task["id"]),
    )
    conn.commit()
    _history(conn, task, "failed", reason)
    logger.warning("tasks scheduler: disabled task %s (%s): %s",
                   task["id"], task["name"], reason)


def _has_active_run(conn, task_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM task_runs WHERE task_id=? AND status IN ('queued','running') LIMIT 1",
        (task_id,),
    ).fetchone()
    return row is not None


def _handle(conn, task, now: int) -> int:
    """Apply the policies to one due task. Returns 1 if a run was enqueued."""
    try:
        next_at = _next_fire(task, now)
    except (cron.CronError, ValueError) as exc:
        _disable(conn, task, f"invalid schedule: {exc}")
        return 0

    missed = (now - int(task["next_run_at"])) > MISSED_AFTER_SECONDS
    if missed and task["catchup_policy"] == "skip":
        # Nothing is recorded for a skipped catchup: the run never existed and
        # a history row per downtime hour would bury the real runs.
        store.set_next_run(conn, task["id"], next_at)
        logger.info("tasks scheduler: task %s missed its window by %ds; "
                    "catchup_policy=skip", task["id"], now - int(task["next_run_at"]))
        return 0

    if task["overlap_policy"] == "skip" and _has_active_run(conn, task["id"]):
        _history(conn, task, "skipped",
                 "previous run still queued or running (overlap_policy=skip)")
        store.set_next_run(conn, task["id"], next_at)
        return 0

    store.create_run(conn, task["id"], task["user_id"], _trigger_of(task))
    store.set_next_run(conn, task["id"], next_at)
    conn.execute("UPDATE scheduled_tasks SET last_run_at=? WHERE id=?",
                 (now, task["id"]))
    conn.commit()
    return 1


def tick_once(conn, *, now: int) -> int:
    """Enqueue every due task's next run. Returns the number enqueued.

    One task's failure never blocks the rest: known-unfixable configuration is
    handled by `_handle` (which disables the task), and anything unexpected is
    logged and stepped over. Deliberately NOT disabling on an unexpected
    exception — a transient DB error would then silently switch off a user's
    working task.
    """
    enqueued = 0
    for task in store.due_tasks(conn, now):
        try:
            enqueued += _handle(conn, task, now)
        except Exception:                       # noqa: BLE001 — never abort the tick
            logger.exception("tasks scheduler: task %s failed to schedule",
                             task["id"])
    return enqueued


async def worker_loop(conn, *, stop_event, tick_seconds: float = TICK_SECONDS) -> None:
    while not stop_event.is_set():
        try:
            n = tick_once(conn, now=int(time.time()))
            if n:
                logger.info("tasks scheduler: enqueued %d run(s)", n)
        except Exception:                       # noqa: BLE001 — never let the loop die
            logger.exception("tasks scheduler tick error")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=tick_seconds)
        except asyncio.TimeoutError:
            pass


def start_worker(conn):
    """Launch the scheduler tick loop; returns (task, stop_event)."""
    stop_event = asyncio.Event()
    task = asyncio.create_task(worker_loop(conn, stop_event=stop_event))
    return task, stop_event
