"""Runner — execute one queued `task_run` end to end, unattended.

`process_once` is the whole story; every side effect is injected (the same
discipline as `notes_distill.process_pending_once`) so the entire ordering
contract is testable without an LLM, the egress-proxy or a real agent run:

    claim_run → get_task → resolve model+credentials → create session →
    attach_session → grant_fs → grant_egress → assemble the run-scoped
    pre-authorization → start_run → TaskRunDriver.drive → finish_run →
    notify.send_result → prune

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
import time

import session_purge
from notes import store as notes_store

from . import grants, notify, preauth, store

logger = logging.getLogger("nimoos-agent.tasks")

# Two runs at a time. Each one is a full agent run (LLM + tools), and this
# process also serves the interactive UI — more parallelism would starve it on
# a CPU-only NAS.
MAX_CONCURRENT = 2

POLL_SECONDS = 5
# Gap between two spawns of the same tick's backlog: long enough not to spin,
# short enough that a queue of due runs drains promptly.
SPAWN_GAP_SECONDS = 0.05

# Strong references to start-up side tasks (asyncio.create_task keeps only a
# weak one, so a collected task would abandon its work half-done).
_STARTUP_TASKS: set = set()

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


# Extension → the interpreter the run gate will accept for it. Only entries
# that are in `skills/shell.py::_SCRIPT_INTERPRETERS`, or the briefing would
# teach a command that gets refused.
_SCRIPT_INTERPRETER_BY_EXT = {
    ".py": "python3", ".sh": "bash", ".bash": "bash", ".zsh": "zsh",
    ".js": "node", ".mjs": "node", ".rb": "ruby", ".pl": "perl",
    ".lua": "lua", ".php": "php",
}

BRIEFING_MAX_CHARS = 1200

# How many scripts to name. A task with fifty pre-authorized scripts is not a
# real shape, and the briefing must not crowd out the author's own prompt.
_BRIEFING_MAX_SCRIPTS = 8


def _shell_max_timeout() -> int:
    """`skills.shell.MAX_TIMEOUT_SEC`, read lazily.

    Lazily because importing the shell skill at module scope would pull the
    whole sandbox stack into every consumer of `tasks.runner`. Read rather than
    duplicated so the briefing can never advise a value the tool would clamp.
    """
    try:
        from skills import shell as _shell  # noqa: PLC0415
        return int(_shell.MAX_TIMEOUT_SEC)
    except Exception:                       # noqa: BLE001 — never break a run
        return 300


def _briefing_command(path: str) -> str:
    ext = path[path.rfind("."):].lower() if "." in path.rsplit("/", 1)[-1] else ""
    interpreter = _SCRIPT_INTERPRETER_BY_EXT.get(ext)
    # No recognized extension: `bash` is the one interpreter that can run an
    # arbitrary executable text file, and it is on the accepted list, so the
    # line we print stays a command the gate would honour.
    return f"{interpreter or 'bash'} {path}"


def format_run_briefing(doc) -> str:
    """Tell the model which scripts it may run, and in exactly what form.

    Found on the live box: with a perfectly good `scripts` grant the model wrote
    ``cd /DATA/AppData/radar && python3 radar.py`` and was refused, because
    chaining is refused whatever the rules say. The grant was right — the model
    simply had no way to learn the required shape. Nothing told it:
    `format_preauth_note` is after-the-fact provenance appended to the summary,
    which the model never sees.

    So this is not a convenience. Without it every author has to discover the
    exact invocation by trial and denial, and the natural first guess (`cd` into
    the directory) is one of the refused shapes.

    Only `scripts` gets a briefing. The other buckets need no shape hint — the
    driver answers their confirmation cards whatever form the request took.
    Returns `""` when there is nothing to say, so a task without a scripts grant
    keeps a byte-identical prompt.
    """
    scripts = []
    if isinstance(doc, dict):
        scripts = [s for s in (doc.get("scripts") or []) if isinstance(s, str) and s]
    if not scripts:
        return ""
    lines = [
        "[This run is pre-authorized to execute the following script(s). "
        "Use the command EXACTLY as written:",
    ]
    for path in scripts[:_BRIEFING_MAX_SCRIPTS]:
        lines.append(f"  {_briefing_command(path)}")
    remaining = len(scripts) - _BRIEFING_MAX_SCRIPTS
    if remaining > 0:
        lines.append(f"  …and {remaining} more (see the task's preauth)")
    lines.append(
        "Any variation is refused: do not prepend `cd`, do not chain with `&&`, "
        "`||`, `;` or a pipe, do not add an argument or a flag, and do not "
        "redirect the output. Read the script's own output from the command "
        "result instead.")
    # Second live finding. The task's `timeout_seconds` is the RUN budget; the
    # shell tool has its OWN per-command timeout that defaults to 30s. The model
    # picked 60 and the collector was killed mid-fetch, so the report came back
    # as a partial log rather than a digest — and nothing in the output said the
    # cause was a per-command timeout. A script worth pre-authorizing usually
    # does real work, so say so, and name the ceiling rather than let the tool
    # clamp a larger value away silently.
    lines.append(
        f"These scripts can take minutes: pass a generous `timeout` on the "
        f"command (up to {_shell_max_timeout()} seconds, the maximum). If the "
        f"output looks cut off mid-way, it was the per-command timeout, not the "
        f"script finishing.]")
    note = "\n".join(lines)
    if len(note) > BRIEFING_MAX_CHARS:
        note = note[:BRIEFING_MAX_CHARS - 2] + "…]"
    return note


def compose_prompt(prompt: str, doc) -> str:
    """The author's prompt, plus the run briefing when there is one.

    The author's text stays FIRST and unmodified: it is the instruction, and the
    briefing is a constraint on how to carry it out.
    """
    briefing = format_run_briefing(doc)
    if not briefing:
        return prompt
    return f"{prompt}\n\n{briefing}" if prompt else briefing


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
    """Where fs-skill snapshots live. Kept as this module's own seam (tests
    monkeypatch it) over `session_purge.default_snapshots_root`."""
    return session_purge.default_snapshots_root()


async def delete_session(conn, user_id: str, session_id: str, *,
                         vector_cleanup=None) -> None:
    """Delete a pruned run's session — thin wrapper over `session_purge`.

    The steps (and the order they have to happen in) live in
    `agent/session_purge.py`, shared with `main.delete_session`'s HTTP
    handler. This used to be a second copy of them, and the copies drifted:
    this one never cleared `agent_runs`/`event_log`, so every pruned run left
    its ~591 event rows behind with nothing pointing at them.
    """
    await session_purge.purge_session(
        conn, user_id, session_id, vector_cleanup=vector_cleanup,
        snapshots_root=_snapshots_root())


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
                       pre_confirmed_tools, run_shell_allowlist,
                       run_scripts=None):
    """Bridge into `main._start_run`, mirroring `main._channel_start_run`."""
    import main  # noqa: PLC0415
    return main._start_run(
        session_id, user_id, message,
        creds["api_key"], creds["base_url"], creds["model"],
        provider_type=creds.get("provider_type", "other"),
        max_turns=max_turns,
        pre_confirmed_tools=pre_confirmed_tools,
        run_shell_allowlist=run_shell_allowlist,
        run_scripts=run_scripts,
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
                       notify=notify.send_result,
                       notify_start=notify.send_start,
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

        # Announce the start only once everything that could still abort the
        # run without ever reaching the agent (missing task, model, or
        # credentials) is behind us — a "started" the user never gets a result
        # for is worse than silence. Its own try/except for the same reason
        # the result notification has one: a channel must never sink a run.
        try:
            await notify_start(conn, task, run)
        except Exception:                   # noqa: BLE001
            logger.warning("tasks runner: start notification failed for run %s",
                           run_id, exc_info=True)

        sink = start_run(
            # The briefing rides the PROMPT rather than a new parameter: it is
            # an instruction to the model, and every start_run seam (including
            # the test double) already carries the prompt.
            session_id, user_id, compose_prompt(task["prompt"], doc), creds,
            max_turns=resolve_max_turns(task, conn, max_turns_reader),
            pre_confirmed_tools=set(doc["mcp_tools"]),
            run_shell_allowlist=doc["shell"],
            run_scripts=doc["scripts"],
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
        # Notification: re-read task+run fresh so it always reflects exactly
        # what finish_run (or the deleted-task/no-model/no-creds early exits)
        # just committed, regardless of which branch above set it. This is
        # the ONE call point for every terminal path, on purpose — a broken
        # notification must never touch the run result already persisted, so
        # it lives in `finally`, wrapped in its own try/except, ahead of the
        # bookkeeping below.
        try:
            notified_task = store.get_task(conn, task_id, user_id)
            if notified_task is not None:
                notified_run = conn.execute(
                    "SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
                if notified_run is not None:
                    await notify(conn, notified_task, notified_run)
        except Exception:                   # noqa: BLE001 — never affect the
            # already-committed run result.
            logger.warning("tasks runner: notify failed for run %s", run_id,
                           exc_info=True)

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


async def _reclaim_orphaned_runs(conn) -> None:
    """Background half of start_worker: clear runs whose task is gone.

    Separate from `requeue_orphaned_runs` (which is sync and only re-labels
    interrupted runs) because deleting a session is async. This is the
    durability net under `store.delete_task`'s background continuation — if the
    process died mid-purge, nothing else would ever find those rows, since every
    other path walks by task_id.
    """
    try:
        n = await store.reclaim_orphaned_runs(conn, session_deleter=delete_session)
        if n > 0:
            logger.info("tasks runner: reclaimed %d run(s) whose task was "
                        "already deleted", n)
    except Exception:                       # noqa: BLE001 — start-up must not
        # depend on this; the next start-up tries again.
        logger.warning("tasks runner: orphaned-run reclaim failed",
                       exc_info=True)


def start_worker(conn):
    """Launch the run worker; returns (task, stop_event)."""
    n = store.requeue_orphaned_runs(conn)
    if n > 0:
        logger.info("tasks runner: marked %d interrupted run(s) failed", n)
    stop_event = asyncio.Event()
    task = asyncio.create_task(worker_loop(conn, stop_event=stop_event))
    # Fired alongside the loop rather than awaited: a slow Parser must not hold
    # up the agent's start-up, and the reclaim races nothing (it only touches
    # rows whose task no longer exists, which no live code path can recreate).
    reclaim = asyncio.create_task(_reclaim_orphaned_runs(conn))
    _STARTUP_TASKS.add(reclaim)
    reclaim.add_done_callback(_STARTUP_TASKS.discard)
    return task, stop_event
