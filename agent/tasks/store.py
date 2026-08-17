"""Store layer for scheduled_tasks / task_runs (M2 scheduled agent tasks).

Two tables, four responsibilities: CRUD on scheduled_tasks (user-scoped),
the due-tasks poll query the scheduler ticks against, an atomic queued->
running claim for task_runs (workers can run concurrently), and pruning old
run history. All timestamps are int seconds (time.time()).
"""
from __future__ import annotations

import json
import logging
import secrets
import time
import uuid

from . import cron

logger = logging.getLogger("nimoos-agent.tasks")

# Fields accepted by create_task, with their column defaults when omitted.
_CREATE_DEFAULTS = {
    "agent_type": "general",
    "model": "",
    "cron_expr": "",
    "interval_seconds": 0,
    "max_turns": 25,
    "timeout_seconds": 1800,
    "overlap_policy": "skip",
    "catchup_policy": "skip",
    "notify_policy": "failure",
    "notify_channel": "",
}

# Fields update_task is allowed to touch. preauth maps to the preauth_json
# column via json.dumps; everything else is a 1:1 column write.
_UPDATABLE_FIELDS = {
    "name", "prompt", "agent_type", "model", "trigger_type", "cron_expr",
    "interval_seconds", "max_turns", "timeout_seconds", "overlap_policy",
    "catchup_policy", "preauth", "notify_policy", "notify_channel", "enabled",
}

# Touching any of these changes when the task next fires, so next_run_at
# must be recomputed from "now" — otherwise a re-enabled task would fire
# using a next_run_at computed before it was disabled, or a cron_expr edit
# would silently keep firing on the old schedule.
_RECOMPUTE_TRIGGERS = {"trigger_type", "cron_expr", "interval_seconds", "enabled"}


def _compute_next_run(trigger_type: str, cron_expr: str, interval_seconds: int,
                       now: int) -> int:
    if trigger_type == "cron":
        return cron.next_after(cron_expr, now)
    if trigger_type == "interval":
        return now + int(interval_seconds)
    return 0  # webhook_only: never fires on its own


def create_task(conn, user_id: str, **fields) -> str:
    task_id = str(uuid.uuid4())
    now = int(time.time())
    webhook_token = secrets.token_hex(16)
    preauth_json = json.dumps(fields.get("preauth", {}))
    trigger_type = fields["trigger_type"]
    cron_expr = fields.get("cron_expr", _CREATE_DEFAULTS["cron_expr"])
    interval_seconds = fields.get("interval_seconds", _CREATE_DEFAULTS["interval_seconds"])
    next_run_at = _compute_next_run(trigger_type, cron_expr, interval_seconds, now)

    def g(key):
        return fields.get(key, _CREATE_DEFAULTS.get(key))

    conn.execute(
        "INSERT INTO scheduled_tasks ("
        "id, user_id, name, prompt, agent_type, model, trigger_type, cron_expr, "
        "interval_seconds, webhook_token, enabled, max_turns, timeout_seconds, "
        "overlap_policy, catchup_policy, preauth_json, notify_policy, "
        "notify_channel, next_run_at, last_run_at, created_at, updated_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,0,?,?)",
        (
            task_id, user_id, fields["name"], fields["prompt"],
            g("agent_type"), g("model"), trigger_type, cron_expr, interval_seconds,
            webhook_token, g("max_turns"), g("timeout_seconds"),
            g("overlap_policy"), g("catchup_policy"), preauth_json,
            g("notify_policy"), g("notify_channel"), next_run_at, now, now,
        ),
    )
    conn.commit()
    return task_id


def get_task(conn, task_id: str, user_id: str):
    return conn.execute(
        "SELECT * FROM scheduled_tasks WHERE id=? AND user_id=?",
        (task_id, user_id),
    ).fetchone()


def list_tasks(conn, user_id: str):
    return conn.execute(
        "SELECT * FROM scheduled_tasks WHERE user_id=? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()


def update_task(conn, task_id: str, user_id: str, **fields) -> bool:
    existing = get_task(conn, task_id, user_id)
    if existing is None:
        return False

    now = int(time.time())
    sets, params, recompute = [], [], False
    for key, value in fields.items():
        if key not in _UPDATABLE_FIELDS:
            continue
        if key == "preauth":
            sets.append("preauth_json=?")
            params.append(json.dumps(value))
        else:
            sets.append(f"{key}=?")
            params.append(value)
        if key in _RECOMPUTE_TRIGGERS:
            recompute = True

    if not sets:
        return True

    sets.append("updated_at=?")
    params.append(now)
    params.extend([task_id, user_id])
    conn.execute(
        f"UPDATE scheduled_tasks SET {', '.join(sets)} WHERE id=? AND user_id=?",
        params,
    )
    conn.commit()

    if recompute:
        merged = get_task(conn, task_id, user_id)
        next_run_at = _compute_next_run(
            merged["trigger_type"], merged["cron_expr"],
            merged["interval_seconds"], now,
        )
        set_next_run(conn, task_id, next_run_at)

    return True


async def delete_task(conn, task_id: str, user_id: str, *,
                      session_deleter=None) -> bool:
    """Delete a task **and everything it produced**.

    The runs have to go first, and they have to go here. `task_runs` has no FK
    cascade to `scheduled_tasks`, and the only path that ever deletes a run row
    (`prune_runs`) walks by `task_id` — so dropping the task row on its own
    strands every run it ever made, each with its session, that session's
    messages, its `agent_runs`/`event_log` rows and its snapshot directory, and
    nothing left in the database points at any of them. Unreclaimable, forever;
    the live box was already carrying 27k such orphan event rows. Same failure
    class as wiki's `file_events` reaching 129M rows.

    `keep=0` is exactly "prune everything", so this reuses `prune_runs` rather
    than repeating its ordering.

    A run that is queued or running for this task is deleted too. That is the
    intent of "delete this task" — but it means an in-flight run will fail on
    its next write to a session that no longer exists. It is already unreadable
    at that point (its history row is gone), so it fails into the runner's
    catch-all and is logged; nothing else observes it.

    This is `async` because deleting a session is (vector cleanup awaits
    Parser). `session_deleter` is `async (conn, user_id, session_id) -> None`,
    defaulting to `tasks.runner.delete_session` — imported lazily because
    `runner` imports this module.
    """
    if get_task(conn, task_id, user_id) is None:
        return False

    if session_deleter is None:
        from .runner import delete_session as session_deleter  # noqa: PLC0415

    for session_id in prune_runs(conn, task_id, keep=0):
        # Per-session try, same reasoning as the runner's prune loop: one
        # undeletable session must not strand the rest — the run rows are
        # already gone, so a skipped session is an orphan nobody comes back
        # for. The task row still goes; a stuck session is not a reason to
        # keep a task the user asked to delete.
        try:
            await session_deleter(conn, user_id, session_id)
        except Exception:                   # noqa: BLE001
            logger.warning("tasks store: could not delete session %s while "
                           "deleting task %s", session_id, task_id,
                           exc_info=True)

    cur = conn.execute(
        "DELETE FROM scheduled_tasks WHERE id=? AND user_id=?",
        (task_id, user_id),
    )
    conn.commit()
    return cur.rowcount > 0


def set_next_run(conn, task_id: str, next_run_at: int) -> None:
    conn.execute(
        "UPDATE scheduled_tasks SET next_run_at=?, updated_at=? WHERE id=?",
        (next_run_at, int(time.time()), task_id),
    )
    conn.commit()


def due_tasks(conn, now_ts: int):
    return conn.execute(
        "SELECT * FROM scheduled_tasks WHERE enabled=1 AND next_run_at>0 "
        "AND next_run_at<=? ORDER BY next_run_at ASC",
        (now_ts,),
    ).fetchall()


def create_run(conn, task_id: str, user_id: str, trigger: str) -> str:
    run_id = str(uuid.uuid4())
    now = int(time.time())
    conn.execute(
        "INSERT INTO task_runs (id, task_id, user_id, session_id, trigger, "
        "status, summary, error, denied_actions, started_at, finished_at, "
        "created_at) VALUES (?,?,?,'',?,'queued','','','[]',0,0,?)",
        (run_id, task_id, user_id, trigger, now),
    )
    conn.commit()
    return run_id


def claim_run(conn):
    """Atomically move the oldest queued run to running.

    Same two-step shape as notes_distill.claim_job: SELECT the candidate,
    then UPDATE guarded by `WHERE status='queued'` so a rowcount of 0 means
    someone else claimed it between the two statements — return None rather
    than a double claim.
    """
    row = conn.execute(
        "SELECT * FROM task_runs WHERE status='queued' "
        "ORDER BY created_at ASC, rowid ASC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    now = int(time.time())
    cur = conn.execute(
        "UPDATE task_runs SET status='running', started_at=? "
        "WHERE id=? AND status='queued'",
        (now, row["id"]),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return conn.execute(
        "SELECT * FROM task_runs WHERE id=?", (row["id"],)
    ).fetchone()


def finish_run(conn, run_id: str, status: str, summary: str = "", error: str = "",
                denied=None) -> None:
    now = int(time.time())
    denied_json = json.dumps(denied) if denied is not None else "[]"
    conn.execute(
        "UPDATE task_runs SET status=?, summary=?, error=?, denied_actions=?, "
        "finished_at=? WHERE id=?",
        (status, summary, error, denied_json, now, run_id),
    )
    conn.commit()


def attach_session(conn, run_id: str, session_id: str) -> None:
    conn.execute(
        "UPDATE task_runs SET session_id=? WHERE id=?", (session_id, run_id)
    )
    conn.commit()


def list_runs(conn, task_id: str, limit: int = 50):
    return conn.execute(
        "SELECT * FROM task_runs WHERE task_id=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (task_id, limit),
    ).fetchall()


def requeue_orphaned_runs(conn) -> int:
    """Mark every queued/running run failed after an agent restart.

    queued must be cleared too, not just running: a queued run's trigger
    time has already passed by the time the process comes back up, and
    re-running it here would race the catchup_policy logic that decides
    whether a missed fire should be replayed. This function only ever
    marks failed — it must never resurrect a run into 'queued' again.
    """
    now = int(time.time())
    cur = conn.execute(
        "UPDATE task_runs SET status='failed', "
        "error='agent restarted mid-run; not retried automatically', "
        "finished_at=? WHERE status IN ('queued','running')",
        (now,),
    )
    conn.commit()
    return cur.rowcount


def prune_runs(conn, task_id: str, keep: int = 50) -> list[str]:
    rows = conn.execute(
        "SELECT id, session_id FROM task_runs WHERE task_id=? "
        "ORDER BY created_at DESC, rowid DESC",
        (task_id,),
    ).fetchall()
    to_drop = rows[keep:]
    dropped_sessions = [r["session_id"] for r in to_drop if r["session_id"]]
    for r in to_drop:
        conn.execute("DELETE FROM task_runs WHERE id=?", (r["id"],))
    conn.commit()
    return dropped_sessions
