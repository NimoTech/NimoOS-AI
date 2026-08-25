"""Store layer for scheduled_tasks / task_runs (M2 scheduled agent tasks).

Two tables, four responsibilities: CRUD on scheduled_tasks (user-scoped),
the due-tasks poll query the scheduler ticks against, an atomic queued->
running claim for task_runs (workers can run concurrently), and pruning old
run history. All timestamps are int seconds (time.time()).
"""
from __future__ import annotations

import asyncio
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
    "notify_on_start": 0,
    # M5's red line lives on this default: a task the AGENT creates must be
    # disabled until a human enables it in the UI, so the agent tool passes
    # enabled=0 explicitly; every other caller keeps today's enabled-on-create.
    "enabled": 1,
}

# Fields update_task is allowed to touch. preauth maps to the preauth_json
# column via json.dumps; everything else is a 1:1 column write.
_UPDATABLE_FIELDS = {
    "name", "prompt", "agent_type", "model", "trigger_type", "cron_expr",
    "interval_seconds", "max_turns", "timeout_seconds", "overlap_policy",
    "catchup_policy", "preauth", "notify_policy", "notify_channel", "enabled",
    "notify_on_start", "allow_prompt_revision",
}

# Every prompt change (agent tool or human PUT) snapshots the version it
# replaced; the history is bounded so a task edited daily for a year cannot
# grow an unbounded audit trail nobody asked for.
PROMPT_REVISIONS_KEEP = 20

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
        "notify_channel, notify_on_start, next_run_at, last_run_at, created_at, "
        "updated_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
        (
            task_id, user_id, fields["name"], fields["prompt"],
            g("agent_type"), g("model"), trigger_type, cron_expr, interval_seconds,
            webhook_token, 1 if g("enabled") else 0,
            g("max_turns"), g("timeout_seconds"),
            g("overlap_policy"), g("catchup_policy"), preauth_json,
            g("notify_policy"), g("notify_channel"),
            1 if g("notify_on_start") else 0, next_run_at, now, now,
        ),
    )
    conn.commit()
    return task_id


def get_task(conn, task_id: str, user_id: str):
    return conn.execute(
        "SELECT * FROM scheduled_tasks WHERE id=? AND user_id=?",
        (task_id, user_id),
    ).fetchone()


def get_task_by_webhook_token(conn, token: str):
    """Look a task up by its webhook token — the ONLY lookup with no user_id.

    The webhook endpoint has no authenticated caller: the token *is* the
    credential, and the owner is whatever this row says. Every other read here
    is scoped by user_id on purpose; this one cannot be, so the caller must
    treat the returned row's `user_id` as authoritative and never take an
    identity from the request.
    """
    if not token:
        return None
    return conn.execute(
        "SELECT * FROM scheduled_tasks WHERE webhook_token=?", (token,),
    ).fetchone()


def reset_webhook_token(conn, task_id: str, user_id: str) -> str:
    """Issue a fresh token, invalidating the old one. '' if not this user's."""
    token = secrets.token_hex(16)
    cur = conn.execute(
        "UPDATE scheduled_tasks SET webhook_token=? WHERE id=? AND user_id=?",
        (token, task_id, user_id),
    )
    conn.commit()
    return token if cur.rowcount > 0 else ""


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

    # A human edit of the prompt supersedes an agent revision: the undo
    # target would be stale, so the revision bookkeeping clears. Only when
    # the text actually changes — editors PUT the prompt back unchanged on
    # every save, and that must not wipe a live revision badge. The replaced
    # version joins the history like any other change.
    if "prompt" in fields and (fields["prompt"] or "") != existing["prompt"]:
        sets.append("prev_prompt=''")
        sets.append("prompt_revised_at=0")
        sets.append("prompt_revised_by=''")
        add_prompt_revision(conn, task_id, user_id, existing["prompt"], "user")

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


def add_prompt_revision(conn, task_id: str, user_id: str, prompt: str,
                        revised_by: str, reason: str = "") -> None:
    """Snapshot the version being REPLACED; prune past the retention cap.

    No commit of its own on the update_task path (it rides that caller's
    transaction); the agent-tool path commits right after calling this.
    """
    conn.execute(
        "INSERT INTO task_prompt_revisions "
        "(task_id, user_id, prompt, revised_by, reason, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (task_id, user_id, prompt, revised_by, reason, int(time.time())))
    conn.execute(
        "DELETE FROM task_prompt_revisions WHERE task_id=? AND id NOT IN "
        "(SELECT id FROM task_prompt_revisions WHERE task_id=? "
        " ORDER BY id DESC LIMIT ?)",
        (task_id, task_id, PROMPT_REVISIONS_KEEP))


def list_prompt_revisions(conn, task_id: str, user_id: str):
    """Newest first. user_id scoping matches every other read here."""
    return conn.execute(
        "SELECT id, prompt, revised_by, reason, created_at "
        "FROM task_prompt_revisions WHERE task_id=? AND user_id=? "
        "ORDER BY id DESC",
        (task_id, user_id)).fetchall()


# Wall-clock budget for one DELETE. The row work is microseconds; the cost is
# the per-session vector cleanup, which awaits Parser behind its own 10s
# timeout — with KEEP_RUNS=50 runs to clear that is up to ~500s spent inside an
# HTTP request, and `route/v2/agent.go`'s ReverseProxy sets no timeout of its
# own. Past the budget the remainder moves to the background (see delete_task).
DELETE_BUDGET_SECONDS = 10

# Sentinel so `budget_seconds` defaults to the CONSTANT ABOVE AS OF CALL TIME,
# not as of import time. A plain default argument would freeze the value, which
# would make the constant unmonkeypatchable and the knob a lie. None stays
# meaningful and distinct: no budget at all.
_DEFAULT_BUDGET = object()

# Strong references to background purge continuations. asyncio.create_task only
# keeps a weak one, so without this a continuation can be garbage collected
# mid-await and the cleanup would be silently dropped.
_PURGE_TASKS: set = set()


async def purge_runs(conn, task_id: str, user_id: str, *, session_deleter,
                     deadline=None, monotonic=time.monotonic) -> bool:
    """Delete every run of `task_id`, one run at a time, session before row.

    Returns True when nothing is left, False when `deadline` (a `monotonic()`
    reading) passed with runs remaining. The deadline is a real bound, not a
    checkpoint: each session's cleanup is capped at whatever is left of it.

    The order inside each iteration is the whole point. "Delete all the run
    rows, then walk the session ids" only works if the walk always finishes:
    the ids live in memory, so an interrupted walk leaves sessions that nothing
    in the database points at — exactly the orphan class this milestone exists
    to close. Deleting one run's session and then that one run row, committing
    as it goes, means any interruption leaves a task with fewer runs, each
    still pointing at its own session: a state a retry finishes.
    """
    while True:
        row = conn.execute(
            "SELECT id, session_id FROM task_runs WHERE task_id=? "
            "ORDER BY created_at ASC, rowid ASC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is None:
            return True
        remaining = None if deadline is None else deadline - monotonic()
        if remaining is not None and remaining <= 0:
            return False
        # A continuation shares its parent's session: leave the session for
        # whichever referencing row goes last (this walk is oldest-first, so
        # the parent row skips and the continuation row deletes).
        if row["session_id"] and not _session_still_referenced(
                conn, row["session_id"], row["id"]):
            try:
                if remaining is None:
                    await session_deleter(conn, user_id, row["session_id"])
                else:
                    # Capped by what is left of the budget, not just checked
                    # after the fact: one session's vector cleanup can itself
                    # take 10s (Parser's own timeout), which would put the
                    # whole delete a full 10s past its deadline. Timing out
                    # here leaves the run row in place, so this run is simply
                    # the continuation's first piece of work.
                    await asyncio.wait_for(
                        session_deleter(conn, user_id, row["session_id"]),
                        timeout=remaining)
            except asyncio.TimeoutError:
                logger.warning("tasks store: session %s of run %s did not "
                               "finish deleting within the remaining budget; "
                               "leaving the run for the continuation",
                               row["session_id"], row["id"])
                return False
            except Exception:               # noqa: BLE001
                # The run row goes anyway. Keeping it would make the SELECT
                # above return the same row forever — one logged orphan session
                # beats a delete that never terminates.
                logger.warning("tasks store: could not delete session %s of "
                               "run %s (task %s); dropping the run row anyway",
                               row["session_id"], row["id"], task_id,
                               exc_info=True)
        conn.execute("DELETE FROM task_runs WHERE id=?", (row["id"],))
        conn.commit()


async def reclaim_orphaned_runs(conn, *, session_deleter) -> int:
    """Delete runs whose task no longer exists, with their sessions.

    The durability net under `delete_task`'s background continuation: if this
    process dies between the task row going and the last run being cleared,
    those runs are unreachable by every other path (`prune_runs` walks by
    task_id, and the task is gone). Called once at worker start-up, so the
    reclaim happens on the next boot at the latest — and it also mops up rows
    left by the versions of this code that had no cascade at all.
    """
    reclaimed = 0
    while True:
        row = conn.execute(
            "SELECT id, user_id, session_id FROM task_runs WHERE task_id NOT IN "
            "(SELECT id FROM scheduled_tasks) ORDER BY rowid LIMIT 1"
        ).fetchone()
        if row is None:
            return reclaimed
        # Same shared-session guard as purge_runs: the last referencing row
        # deletes the session.
        if row["session_id"] and not _session_still_referenced(
                conn, row["session_id"], row["id"]):
            try:
                await session_deleter(conn, row["user_id"], row["session_id"])
            except Exception:               # noqa: BLE001 — same trade as
                # purge_runs: never spin on one bad row.
                logger.warning("tasks store: could not delete session %s of "
                               "orphaned run %s", row["session_id"], row["id"],
                               exc_info=True)
        conn.execute("DELETE FROM task_runs WHERE id=?", (row["id"],))
        conn.commit()
        reclaimed += 1


def _spawn(coro) -> None:
    """Run `coro` detached, keeping a strong reference. No loop = run inline is
    NOT an option here (the caller is already inside one); a missing loop only
    happens in a sync test, where the coroutine is closed rather than leaked."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        logger.warning("tasks store: no running loop; purge continuation "
                       "skipped (the next worker start-up reclaims it)")
        return
    handle = loop.create_task(coro)
    _PURGE_TASKS.add(handle)
    handle.add_done_callback(_PURGE_TASKS.discard)


async def delete_task(conn, task_id: str, user_id: str, *,
                      session_deleter=None,
                      budget_seconds=_DEFAULT_BUDGET,
                      monotonic=time.monotonic, spawn=None) -> bool:
    """Delete a task **and everything it produced**, in bounded time.

    The runs have to go, and they have to go here. `task_runs` has no FK
    cascade to `scheduled_tasks`, and the only other path that deletes a run row
    (`prune_runs`) walks by `task_id` — so dropping the task row on its own
    strands every run it ever made, each with its session, that session's
    messages, its `agent_runs`/`event_log` rows and its snapshot directory, and
    nothing left in the database points at any of them. Unreclaimable, forever;
    the live box was already carrying 27k such orphan event rows. Same failure
    class as wiki's `file_events` reaching 129M rows.

    Bounded, because this runs inside an HTTP request: `purge_runs` gets
    `budget_seconds`, and if it runs out the task row still goes (the caller
    asked for the task to be gone) and the remaining runs — still in the table,
    still pointing at their own sessions — are finished by a background
    continuation, or by `reclaim_orphaned_runs` at the next worker start-up if
    this process does not live that long. Nothing is dropped silently; every
    hand-off is logged.

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

    if budget_seconds is _DEFAULT_BUDGET:
        budget_seconds = DELETE_BUDGET_SECONDS
    # Resolved here, not as a default argument: a default would capture this
    # module's function object at import time, past the reach of a monkeypatch.
    spawn = spawn or _spawn
    deadline = None if budget_seconds is None else monotonic() + budget_seconds
    done = await purge_runs(conn, task_id, user_id,
                            session_deleter=session_deleter, deadline=deadline,
                            monotonic=monotonic)

    conn.execute("DELETE FROM task_prompt_revisions WHERE task_id=?",
                 (task_id,))
    cur = conn.execute(
        "DELETE FROM scheduled_tasks WHERE id=? AND user_id=?",
        (task_id, user_id),
    )
    conn.commit()

    if not done:
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM task_runs WHERE task_id=?",
            (task_id,)).fetchone()["c"]
        logger.warning("tasks store: delete of task %s exceeded its %ss budget "
                       "with %d run(s) left; continuing in the background",
                       task_id, budget_seconds, remaining)
        spawn(purge_runs(conn, task_id, user_id,
                         session_deleter=session_deleter, deadline=None))

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


def create_continue_run(conn, task_id: str, user_id: str, *, session_id: str,
                        resumed_from: str, resume_message: str) -> str:
    """Queue a continuation of a finished run, on that run's own session.

    `session_id` is pre-attached HERE, not by the runner: a non-empty
    `resumed_from` is what tells `process_once` to reuse the session instead
    of minting a fresh one. The trigger stays 'manual' — the column's CHECK
    constraint cannot grow a value without a table rebuild, and a continuation
    is a human action anyway.
    """
    run_id = str(uuid.uuid4())
    now = int(time.time())
    conn.execute(
        "INSERT INTO task_runs (id, task_id, user_id, session_id, trigger, "
        "status, summary, error, denied_actions, resumed_from, resume_message, "
        "started_at, finished_at, created_at) "
        "VALUES (?,?,?,?,'manual','queued','','','[]',?,?,0,0,?)",
        (run_id, task_id, user_id, session_id, resumed_from, resume_message,
         now),
    )
    conn.commit()
    return run_id


def session_active_run(conn, session_id: str):
    """The queued/running run attached to `session_id`, if any.

    Two runs writing one session's history concurrently would corrupt it, so
    the continue endpoint refuses while this returns a row.
    """
    if not session_id:
        return None
    return conn.execute(
        "SELECT * FROM task_runs WHERE session_id=? "
        "AND status IN ('queued','running') LIMIT 1",
        (session_id,),
    ).fetchone()


def _session_still_referenced(conn, session_id: str, excluding_run_id) -> bool:
    """True if any OTHER task_runs row points at this session.

    Continuation runs share their parent's session, so every deletion path
    must ask this before deleting a session — otherwise dropping either run
    row kills the transcript the surviving row still shows.
    """
    row = conn.execute(
        "SELECT 1 FROM task_runs WHERE session_id=? AND id!=? LIMIT 1",
        (session_id, excluding_run_id),
    ).fetchone()
    return row is not None


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
    for r in to_drop:
        conn.execute("DELETE FROM task_runs WHERE id=?", (r["id"],))
    conn.commit()
    # A continuation run shares its parent's session, so a session is only
    # safe to delete once NO surviving run references it — and it must be
    # handed back at most once even when several dropped rows shared it.
    dropped_sessions: list[str] = []
    seen: set[str] = set()
    for r in to_drop:
        sid = r["session_id"]
        if not sid or sid in seen:
            continue
        seen.add(sid)
        if not _session_still_referenced(conn, sid, r["id"]):
            dropped_sessions.append(sid)
    return dropped_sessions
