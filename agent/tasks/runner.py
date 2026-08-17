"""Runner — execute one queued `task_run` end to end, unattended.

`process_once` is the whole story; every side effect is injected (the same
discipline as `notes_distill.process_pending_once`) so the entire ordering
contract is testable without an LLM, the egress-proxy or a real agent run:

    claim_run → get_task → resolve model+credentials → create session →
    attach_session → grant_fs → grant_egress → assemble the run-scoped
    pre-authorization → start_run → TaskRunDriver.drive → finish_run → prune

The order is not cosmetic:

* `attach_session` comes BEFORE the grants, so a crash mid-grant leaves rows
  that can still be traced back to a run (and cleaned up by pruning).
  `visible_resources` rows for a session no run points at would be invisible
  garbage.
* Both grants come BEFORE `start_run`. The agent task starts executing the
  moment `_start_run` returns, so a grant registered afterwards races the
  first tool call.
* `pre_confirmed_tools` / `run_shell_allowlist` are passed INTO `start_run`
  rather than set afterwards, because `AgentRunner.run` seeds the contextvars
  itself (agent.py) — anything set from out here would be overwritten.

Three things happen after the driver returns that are easy to miss, and each of
them leaks something permanently if skipped: a timed-out run is CANCELLED (the
driver only stops watching it), its sink is EVICTED from `main._active_runs`
(which only ever inserts), and pruned sessions are deleted messages-first,
together with their vectors and snapshots (see `delete_session`).

Nothing in here raises. A scheduled run has nobody watching, so every failure
mode has to land in the run row as a status a human can read later.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time

from notes import store as notes_store

from . import grants, preauth, store

logger = logging.getLogger("nimoos-agent.tasks")

# Two runs at a time. Each one is a full agent run (LLM + tools), and this
# process also serves the interactive UI — more parallelism would starve it on
# a CPU-only NAS.
MAX_CONCURRENT = 2

POLL_SECONDS = 5
# Gap between two spawns of the same tick's backlog: long enough not to spin,
# short enough that a queue of due runs drains promptly.
SPAWN_GAP_SECONDS = 0.05

# Run history kept per task; older rows (and the sessions they own) are pruned
# after every run.
KEEP_RUNS = 50

# How long to wait for a cancelled run to actually unwind before moving on.
# Same 5s as main.cancel_session.
CANCEL_GRACE_SECONDS = 5.0

# Fallback for a task whose timeout_seconds is 0 or negative. Deliberately NOT
# "0 means no limit" — that reading is right for max_turns (and is what
# resolve_max_turns implements) but inverted here: a 0 deadline makes
# TaskRunDriver return `timeout` on its very first check, so every run of such
# a task would fail instantly. The column's own default is 1800.
DEFAULT_TIMEOUT_SECONDS = 1800

# Cap for the "[preauth used: …]" footer, so a run that auto-approved hundreds
# of cards cannot bloat the summary a UI has to render.
_NOTE_MAX_CHARS = 500


def format_preauth_note(auto_approved) -> str:
    """Render the actions the driver auto-approved from the task's document.

    Deliberately NOT a new column: this is provenance for a human reading a
    run ("the network access it made was pre-authorized, not a bug"), and the
    machine-readable half of the same story already exists as `denied_actions`
    for the refusals. Grouped by kind, deduplicated, order preserved.
    """
    if not auto_approved:
        return ""
    groups: dict[str, list[str]] = {}
    for item in auto_approved:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "unknown")
        detail = str(item.get("detail") or "").strip()
        bucket = groups.setdefault(kind, [])
        if detail and detail not in bucket:
            bucket.append(detail)
    if not groups:
        return ""
    parts = [f"{kind} {', '.join(details)}" if details else kind
             for kind, details in groups.items()]
    note = "[preauth used: " + "; ".join(parts) + "]"
    if len(note) > _NOTE_MAX_CHARS:
        note = note[:_NOTE_MAX_CHARS - 2] + "…]"
    return note


def _redact(msg: str, creds) -> str:
    key = (creds or {}).get("api_key") if isinstance(creds, dict) else None
    if key:
        msg = msg.replace(key, "***")
    return msg


def resolve_max_turns(task, conn, max_turns_reader) -> "int | None":
    """The task's own `max_turns`, falling back to the user-level setting.

    Same semantics as `main._read_max_turns_setting` on both sides: 0 means
    unlimited (→ None, which is what `_start_run` wants). The column is NOT
    NULL with a default, so the fallback only fires for a hand-edited row.
    """
    raw = task["max_turns"]
    if raw is None or int(raw) < 0:
        raw = max_turns_reader(conn, task["user_id"]) if max_turns_reader else 10
    raw = int(raw)
    return None if raw == 0 else raw


def _snapshots_root() -> str:
    """Where fs-skill snapshots live. Resolved through `main` so a test (or a
    non-default deployment) that moves the root is honored here too."""
    try:
        import main  # noqa: PLC0415
        return main._snapshots_root
    except Exception:                       # noqa: BLE001 — main not importable
        return os.environ.get("AGENT_SNAPSHOTS_ROOT",
                              "/var/lib/nimoos/ai/agent/snapshots")


async def _default_vector_cleanup(user_id: str, session_id: str) -> None:
    import recall_index  # noqa: PLC0415
    await asyncio.wait_for(
        recall_index._get_parser_client().agent_memory_delete(user_id, session_id),
        timeout=10)


async def delete_session(conn, user_id: str, session_id: str, *,
                         vector_cleanup=None) -> None:
    """Delete a session the way `main.delete_session` does — all four steps.

    `messages.session_id` is `REFERENCES sessions(id)` **without** ON DELETE
    CASCADE (db.py) and `PRAGMA foreign_keys=ON` is set at init, so deleting
    the session row first raises `IntegrityError: FOREIGN KEY constraint
    failed` for any session that ever exchanged a message — i.e. every real
    task run. Messages go first.

    The other two steps are not optional either: the recall vectors would keep
    a pruned run's content answerable by `recall`, and the snapshot directory
    would leak disk for the lifetime of the box.
    """
    owner = conn.execute("SELECT user_id FROM sessions WHERE id=?",
                         (session_id,)).fetchone()
    if owner is not None and str(owner["user_id"]) != str(user_id):
        # main.delete_session scopes its DELETE by user_id; the equivalent here
        # is refusing outright, because a run row pointing at someone else's
        # session is corruption, not a permission question.
        logger.warning("tasks runner: refusing to delete session %s owned by "
                       "another user", session_id)
        return
    cleanup = vector_cleanup or _default_vector_cleanup
    try:
        await cleanup(user_id, session_id)
    except Exception as exc:                # noqa: BLE001 — soft-fail, same as
        # main.delete_session: deletion must never depend on Parser being up;
        # a missed cleanup only leaves orphan vectors (logged).
        logger.warning("tasks runner: vector cleanup failed for session %s: %s",
                       session_id, exc)
    conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    conn.commit()
    shutil.rmtree(os.path.join(_snapshots_root(), session_id), ignore_errors=True)


async def cancel_sink(sink) -> bool:
    """Stop the agent task behind `sink`. Returns True if it was still running.

    The driver's `cancel_session` only answers pending confirmation cards — it
    does NOT stop the run (see main.py's /cancel endpoint, which cancels the
    task for exactly this reason). Without this, a timed-out task keeps burning
    the box's CPU, and — because its run row is already terminal —
    `overlap_policy='skip'` would happily enqueue the next fire alongside it.

    `CANCEL_GRACE_SECONDS` is a real bound here, and it has to be:
    `asyncio.wait_for` would NOT give one. On its own timeout `wait_for` runs
    `_cancel_and_wait(fut)`, i.e. it cancels and then waits for the task to
    unwind **without any deadline** — so a run that swallows `CancelledError`
    (blocking sync code, a subprocess, a tool catching BaseException) parks
    this coroutine forever. That failure is silent and terminal for the worker:
    `slot()` never releases its semaphore, the queue stops draining, and every
    frame involved sits inside a try/except, so there is no exception and no
    log to notice. `asyncio.wait` returns at the deadline and leaves the task
    pending instead, which is the trade we want — the run is already doomed;
    the worker must not be.
    """
    task = getattr(sink, "task", None)
    if task is None or task.done():
        return False
    task.cancel()
    done, pending = await asyncio.wait({task}, timeout=CANCEL_GRACE_SECONDS)
    if pending:
        logger.warning("tasks runner: run did not unwind within %.0fs of being "
                       "cancelled; leaving it to finish on its own",
                       CANCEL_GRACE_SECONDS)
    for finished in done:
        if finished.cancelled():
            continue                        # the cancellation we just asked for
        try:
            finished.exception()            # retrieve, so asyncio stays quiet
        except Exception:                   # noqa: BLE001
            pass
    return True


def evict_sink(session_id: str, sink=None) -> None:
    """Drop this run's sink from `main._active_runs`.

    `_start_run` only ever inserts (main.py); the web path keeps the sink
    around so a late reconnect can replay the stream. A scheduled run has no
    reconnecting client, so leaving it there pins every event of every run in
    memory forever — a 5-minute task alone is 288 sinks/day.
    """
    try:
        import main  # noqa: PLC0415
    except Exception:                       # noqa: BLE001
        return
    current = main._active_runs.get(session_id)
    if current is None:
        return
    if sink is not None and current is not sink:
        return                              # someone else's run; leave it alone
    main._active_runs.pop(session_id, None)


# -- default (production) dependencies ---------------------------------------
#
# Imported lazily inside each function: `main` imports this module at startup,
# so a module-level `import main` would be circular.

def _default_session_factory(conn, user_id: str, agent_type: str) -> str:
    from channels.store import create_channel_session  # noqa: PLC0415
    session_id = create_channel_session(conn, user_id, "task")
    if agent_type and agent_type != "general":
        # create_channel_session hardcodes 'general'; a task can pick another
        # agent type, and the runner is the only caller that needs to.
        conn.execute("UPDATE sessions SET agent_type=? WHERE id=?",
                     (agent_type, session_id))
        conn.commit()
    return session_id


async def _default_creds_resolver(user_id: str, model: str):
    from channels import credentials  # noqa: PLC0415
    return await credentials.resolve(user_id, model)


def _default_start_run(session_id, user_id, message, creds, *, max_turns,
                       pre_confirmed_tools, run_shell_allowlist):
    """Bridge into `main._start_run`, mirroring `main._channel_start_run`."""
    import main  # noqa: PLC0415
    return main._start_run(
        session_id, user_id, message,
        creds["api_key"], creds["base_url"], creds["model"],
        provider_type=creds.get("provider_type", "other"),
        max_turns=max_turns,
        pre_confirmed_tools=pre_confirmed_tools,
        run_shell_allowlist=run_shell_allowlist,
    )


def _default_driver_factory(*, session_id, preauth, run_timeout):
    import main  # noqa: PLC0415

    from .driver import TaskRunDriver  # noqa: PLC0415
    return TaskRunDriver(confirm_mgr=main._confirm_mgr, session_id=session_id,
                         preauth=preauth, run_timeout=run_timeout)


def _default_max_turns_reader(conn, user_id: str) -> int:
    import main  # noqa: PLC0415
    return main._read_max_turns_setting(conn, user_id)


# -- one run -----------------------------------------------------------------

async def process_once(conn, *, start_run, creds_resolver, driver_factory,
                       session_factory, now=None,
                       grant_fs=grants.grant_fs,
                       grant_egress=grants.grant_egress,
                       prune=store.prune_runs,
                       session_deleter=None,
                       cancel=cancel_sink,
                       evict=evict_sink,
                       max_turns_reader=_default_max_turns_reader,
                       keep_runs: int = KEEP_RUNS) -> bool:
    """Claim and execute at most one run. False only when there was nothing
    to do — the caller uses that to decide whether to sleep.

    `now` is accepted for shape symmetry with `notes_distill`'s worker and to
    keep the seam open; it is currently unused, because every write on this
    path (`claim_run`, `finish_run`) stamps its own timestamp inside the store.
    """
    run = store.claim_run(conn)
    if run is None:
        return False

    run_id, task_id, user_id = run["id"], run["task_id"], run["user_id"]
    creds = None
    task = None
    sink = None
    session_id = ""
    session_deleter = session_deleter or delete_session
    try:
        task = store.get_task(conn, task_id, user_id)
        if task is None:
            # Deleted while queued. Nothing to run and nothing to retry.
            store.finish_run(conn, run_id, "failed",
                             error="task no longer exists")
            return True

        model = (task["model"] or "").strip() or \
            notes_store.get_background_model(conn, user_id)
        if not model:
            # Loud, not silent: a task with no model is a configuration bug the
            # user has to see in the run history, not a run quietly skipped.
            store.finish_run(conn, run_id, "failed",
                             error="no model configured")
            return True

        creds = await creds_resolver(user_id, model)
        if not creds:
            store.finish_run(
                conn, run_id, "failed",
                error=f"credentials unresolved for model {model!r}")
            return True

        doc = preauth.parse(task["preauth_json"])

        session_id = session_factory(conn, user_id, task["agent_type"])
        store.attach_session(conn, run_id, session_id)

        try:
            granted = grant_fs(conn, session_id, doc["fs_write"])
            if granted:
                logger.info("tasks runner: granted %d fs path(s) to session %s",
                            granted, session_id)
        except Exception:                   # noqa: BLE001
            # A failed grant degrades to the normal confirmation gate (which
            # this driver then denies and records) — it must not sink the run,
            # whose other work may not need the filesystem at all.
            logger.warning("tasks runner: grant_fs failed for session %s",
                           session_id, exc_info=True)
        try:
            await grant_egress(doc["egress_domains"])
        except Exception:                   # noqa: BLE001
            # Same reasoning, plus: grant_egress only pre-pays an upload byte
            # budget. Without it the proxy simply asks again mid-upload.
            logger.warning("tasks runner: grant_egress failed", exc_info=True)

        sink = start_run(
            session_id, user_id, task["prompt"], creds,
            max_turns=resolve_max_turns(task, conn, max_turns_reader),
            pre_confirmed_tools=set(doc["mcp_tools"]),
            run_shell_allowlist=doc["shell"],
        )

        timeout_seconds = int(task["timeout_seconds"] or 0)
        if timeout_seconds <= 0:
            timeout_seconds = DEFAULT_TIMEOUT_SECONDS
        driver = driver_factory(session_id=session_id, preauth=doc,
                                run_timeout=timeout_seconds)
        result = await driver.drive(sink) or {}

        status = str(result.get("status") or "failed")
        if status == "timeout":
            # The wall-clock watchdog: the driver stopped WATCHING the run, the
            # run itself is still going. Kill it before recording the timeout —
            # otherwise it burns CPU to completion and the next scheduled fire
            # starts alongside it despite overlap_policy='skip' (the run row is
            # already terminal, so nothing counts it as active any more).
            if await cancel(sink):
                logger.warning("tasks runner: cancelled run %s after %ds",
                               run_id, timeout_seconds)

        summary = str(result.get("summary") or "")
        note = format_preauth_note(result.get("auto_approved") or [])
        if note:
            summary = f"{summary}\n\n{note}" if summary else note
        store.finish_run(conn, run_id, status,
                         summary=summary, error=str(result.get("error") or ""),
                         denied=result.get("denied") or [])
        return True
    except Exception as exc:                # noqa: BLE001 — never escape
        msg = _redact(str(exc) or type(exc).__name__, creds)
        logger.exception("tasks runner: run %s failed: %s", run_id, msg)
        if sink is not None:
            # The agent task outlives whatever broke out here (a driver bug, a
            # DB error while recording): same reasoning as the timeout path.
            try:
                await cancel(sink)
            except Exception:               # noqa: BLE001
                logger.warning("tasks runner: cancel after failure did not "
                               "complete for run %s", run_id, exc_info=True)
        try:
            store.finish_run(conn, run_id, "failed", error=msg)
        except Exception:                   # noqa: BLE001
            logger.exception("tasks runner: could not record failure for run %s",
                             run_id)
        return True
    finally:
        # Bookkeeping: it runs whatever happened above, and no failure in here
        # may undo the finish_run that just landed.
        if sink is not None and session_id:
            try:
                evict(session_id, sink)
            except Exception:               # noqa: BLE001
                logger.warning("tasks runner: could not evict sink for %s",
                               session_id, exc_info=True)
        try:
            dropped = prune(conn, task_id, keep=keep_runs) or ()
        except Exception:                   # noqa: BLE001
            logger.warning("tasks runner: prune failed for task %s", task_id,
                           exc_info=True)
            dropped = ()
        for old_session in dropped:
            # Per-session try: one undeletable session (a locked snapshot dir,
            # a Parser hiccup) must not strand the rest of the batch — the run
            # rows are already gone, so a skipped session is an orphan nobody
            # will ever come back for.
            try:
                await session_deleter(conn, user_id, old_session)
            except Exception:               # noqa: BLE001
                logger.warning("tasks runner: could not delete pruned session %s",
                               old_session, exc_info=True)


async def _process_with_defaults(conn) -> bool:
    return await process_once(
        conn, start_run=_default_start_run,
        creds_resolver=_default_creds_resolver,
        driver_factory=_default_driver_factory,
        session_factory=_default_session_factory,
        now=int(time.time()),
    )


# -- worker ------------------------------------------------------------------

def _queued_waiting(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM task_runs WHERE status='queued' LIMIT 1").fetchone() is not None


async def worker_loop(conn, *, stop_event, process=None,
                      poll_seconds: float = POLL_SECONDS,
                      max_concurrent: int = MAX_CONCURRENT) -> None:
    """Spawn up to `max_concurrent` runs, one per queued row.

    The peek-then-spawn shape (rather than awaiting `process` inline) is what
    lets two 30-minute runs overlap without the second waiting for the first.
    The semaphore is the only concurrency bound; `backoff` exists so that a
    `process` that raises *before* claiming its row — the one case where the
    queued row survives the attempt — cannot turn into a spawn loop.
    """
    process = process or _process_with_defaults
    sem = asyncio.Semaphore(max_concurrent)
    inflight: set[asyncio.Task] = set()
    state = {"backoff": False}

    async def slot():
        try:
            await process(conn)
        except Exception:                   # noqa: BLE001 — a child must not
            # take the loop down with it, and its exception must be retrieved
            # here or asyncio logs it as "never retrieved" at GC time.
            logger.exception("tasks runner: run attempt failed")
            state["backoff"] = True
        finally:
            sem.release()

    while not stop_event.is_set():
        delay = poll_seconds
        try:
            if not state["backoff"] and not sem.locked() and _queued_waiting(conn):
                await sem.acquire()
                t = asyncio.create_task(slot())
                inflight.add(t)
                t.add_done_callback(inflight.discard)
                delay = SPAWN_GAP_SECONDS
        except Exception:                   # noqa: BLE001 — never let the loop die
            logger.exception("tasks runner tick error")
        state["backoff"] = False
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    # In-flight runs are deliberately NOT awaited on shutdown: a task run can
    # legitimately last its full timeout_seconds, and blocking the process exit
    # on it would hang systemd's restart. store.requeue_orphaned_runs (called
    # from start_worker) marks whatever was interrupted as failed on the way
    # back up, and never replays it.
    if inflight:
        logger.info("tasks runner: stopping with %d run(s) in flight",
                    len(inflight))


def start_worker(conn):
    """Launch the run worker; returns (task, stop_event)."""
    n = store.requeue_orphaned_runs(conn)
    if n > 0:
        logger.info("tasks runner: marked %d interrupted run(s) failed", n)
    stop_event = asyncio.Event()
    task = asyncio.create_task(worker_loop(conn, stop_event=stop_event))
    return task, stop_event
