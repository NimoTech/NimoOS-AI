"""RunSink — per-run event log + pubsub.

The agent run is detached from any single HTTP connection. Events flow from
the agent task into a RunSink, which:

  1. Persists each event to `event_log` so reconnecting clients can replay.
  2. Fans the event out to every live subscriber (multiple browser tabs,
     reconnect after disconnect, etc.).

Drop-in for asyncio.Queue: exposes async `put(event)`, so existing skill code
that does `await queue.put({...})` works unchanged.

Persistence coalesces streaming deltas (spec §8 / P5, 2026-09-07): a
DeepSeek-class run emits 50-70k `thinking` deltas, and one synchronous SQLite
commit per delta starved the event loop (118: /agent/health took 25 s while a
run with four parallel sub-agents streamed). Consecutive `thinking` /
`message_delta` events are merged into ONE row per UI scope — the parent
stream is one scope, each sub-agent (`subagent_event.parent_call_id`) is
another, so four children streaming round-robin still coalesce (a single
buffer would flush on every alternation and persist one row per delta again).
Within a scope the buffer flushes when the delta type changes or exceeds
COALESCE_MAX_CHARS; ALL buffers flush, in insertion order, when a
non-delta event is persisted (tool calls / cards / done stay ordered against
every stream), on subscribe(), on the terminal event, and FLUSH_INTERVAL
seconds after a buffer was opened (max-age, bounding crash-window loss; not
an idle timer). Only deltas of different scopes may reorder relative to each
other, and those render into different blocks. Live subscribers still
receive every raw delta; only the replay log (`_past`, `event_log`) is
coalesced — the UI reducers append deltas, so a merged delta renders
identically.

Invariant (load-bearing): put() must never suspend between the coalescing
decision and _persist(), and fan-out must not await either — subscriber
queues are unbounded and fed with put_nowait(), so the live order always
matches the persisted order. Do not add an await to put().
"""

import asyncio
import json
import logging
import os
import sqlite3
import time

_LOG = logging.getLogger("nimoos-agent.run_sink")

_TERMINAL_TYPES = ("done",)
COALESCE_TYPES = frozenset({"thinking", "message_delta"})
FLUSH_INTERVAL = 0.25          # max age of an open delta buffer (seconds)
COALESCE_MAX_CHARS = 4000      # flush a scope when its merged content grows past this


def _ttl_days(raw: str | None, default: int = 30) -> int:
    """Parse NIMOOS_EVENT_LOG_TTL_DAYS defensively: garbage or <1 → default
    (a bad value must not stop the agent from starting, and 0/negative would
    delete in-flight runs' rows)."""
    try:
        v = int(str(raw).strip()) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default
    return v if v >= 1 else default


EVENT_LOG_TTL_DAYS = _ttl_days(os.environ.get("NIMOOS_EVENT_LOG_TTL_DAYS"))
SWEEP_BATCH = 20000            # rows per DELETE batch (bounds the WAL spike)


def _scope(event: dict) -> str:
    """UI scope of a delta: "" for the parent stream, the parent_call_id for a
    sub-agent's wrapped stream."""
    return str(event.get("parent_call_id", "")) if event.get("type") == "subagent_event" else ""


def _coalesce_key(event: dict):
    """Identity of the delta stream this event belongs to, or None when the
    event must be persisted as its own row."""
    t = event.get("type")
    if t in COALESCE_TYPES and isinstance(event.get("content"), str):
        return (t,)
    if t == "subagent_event":
        inner = event.get("event")
        if isinstance(inner, dict) and inner.get("type") in COALESCE_TYPES \
                and isinstance(inner.get("content"), str):
            return ("subagent_event", str(event.get("parent_call_id", "")), inner["type"])
    return None


def _content_len(event: dict) -> int:
    inner = event.get("event") if event.get("type") == "subagent_event" else event
    return len(str(inner.get("content", "")))


def _merge_into(pending: dict, event: dict) -> None:
    if pending.get("type") == "subagent_event":
        pending["event"]["content"] += event["event"]["content"]
    else:
        pending["content"] += event["content"]


class RunSink:
    def __init__(self, run_id: str, session_id: str, db: sqlite3.Connection):
        self.run_id = run_id
        self.session_id = session_id
        self._db = db
        self._past: list[dict] = []
        self._subscribers: list[asyncio.Queue] = []
        self._seq = 0
        self._done = False
        # Set by main.py after spawning the agent task. /cancel calls
        # task.cancel() to release the per-session lock so the next /run
        # isn't rejected with agent_busy.
        self.task: asyncio.Task | None = None
        # Coalescing state: per UI scope, the delta row being accumulated (a
        # deep-enough copy of the first event) and its stream key; dict order
        # = insertion order = flush order. One max-age timer for all buffers.
        self._pending: dict[str, tuple[tuple, dict]] = {}
        self._flush_handle: asyncio.TimerHandle | None = None

    async def put(self, event: dict) -> None:
        """Fan out immediately; persist coalesced (see module docstring).
        No await between here and _persist(); fan-out uses put_nowait()."""
        key = _coalesce_key(event)
        if key is not None:
            scope = _scope(event)
            cur = self._pending.get(scope)
            if cur is not None and cur[0] == key \
                    and _content_len(cur[1]) + _content_len(event) <= COALESCE_MAX_CHARS:
                _merge_into(cur[1], event)
            else:
                if cur is not None:
                    self._flush_scope(scope)
                self._pending[scope] = (key, self._copy_delta(event))
                self._arm_timer()
        else:
            self.flush()
            self._persist(event)
        if event.get("type") in _TERMINAL_TYPES:
            self.flush()
            self._done = True
        for sub in list(self._subscribers):
            sub.put_nowait(event)          # unbounded queues: never raises, never yields

    @staticmethod
    def _copy_delta(event: dict) -> dict:
        c = dict(event)
        if c.get("type") == "subagent_event" and isinstance(c.get("event"), dict):
            c["event"] = dict(c["event"])
        return c

    def _arm_timer(self) -> None:
        if self._flush_handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._flush_handle = loop.call_later(FLUSH_INTERVAL, self._on_timer)

    def _on_timer(self) -> None:
        self._flush_handle = None
        self.flush()

    def flush(self) -> None:
        """Persist every pending delta buffer, in insertion order. Sync; safe
        to call often."""
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        for scope in list(self._pending):
            self._flush_scope(scope)

    def _flush_scope(self, scope: str) -> None:
        cur = self._pending.pop(scope, None)
        if cur is not None:
            self._persist(cur[1])

    def _persist(self, event: dict) -> None:
        """One event_log row + one `_past` entry. Persistence is best-effort —
        a transient SQLite error must not drop an event the user is watching
        live (fan-out happens regardless)."""
        self._seq += 1
        seq = self._seq
        try:
            self._db.execute(
                "INSERT INTO event_log (run_id, seq, payload, created_at) VALUES (?,?,?,?)",
                (self.run_id, seq, json.dumps(event), int(time.time())),
            )
            self._db.commit()
        except Exception:
            pass
        self._past.append(event)

    def subscribe(self) -> tuple[list[dict], asyncio.Queue]:
        """Atomic past+queue. Caller drains past first, then awaits queue.

        The list copy and queue registration happen with no awaits in between,
        so the subscriber cannot miss any event emitted between snapshot and
        registration.
        """
        self.flush()   # a late joiner must see the buffered deltas in `past`
        q: asyncio.Queue = asyncio.Queue()
        past_snapshot = list(self._past)
        self._subscribers.append(q)
        return past_snapshot, q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    @property
    def is_done(self) -> bool:
        return self._done


def load_events_from_db(db: sqlite3.Connection, run_id: str) -> list[dict]:
    """Read all logged events for a run, in seq order. Used when the in-memory
    sink is gone (e.g. after process restart) but we still want to replay."""
    rows = db.execute(
        "SELECT payload FROM event_log WHERE run_id=? ORDER BY seq ASC",
        (run_id,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            out.append(json.loads(r["payload"]))
        except Exception:
            continue
    return out


def _sweep_batch(db: sqlite3.Connection, cutoff: int) -> int:
    cur = db.execute(
        "DELETE FROM event_log WHERE rowid IN "
        "(SELECT rowid FROM event_log WHERE created_at < ? LIMIT ?)", (cutoff, SWEEP_BATCH))
    db.commit()
    try:
        db.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:  # noqa: BLE001
        pass
    return int(cur.rowcount or 0)


def sweep_event_log(db: sqlite3.Connection, *, ttl_days: int | None = None,
                    now: float | None = None) -> int:
    """Delete event_log rows older than ttl_days in SWEEP_BATCH-row batches
    (each committed + PASSIVE-checkpointed, so the WAL stays small). Rows are
    the replay source for the run-stream and the task transcript; tasks keep
    their last 50 runs but nothing ever reclaimed ordinary chat runs' rows
    (1.76 M rows / 326 MB on 118 by 2026-09-07). event_log is indexed only by
    (run_id, seq): the created_at scan is deliberate — after the coalescing
    above the table is small and an index would sit on the hot insert path.
    Freed pages are reused, not returned to the filesystem (auto_vacuum off);
    a VACUUM needs free space equal to the DB, which the box may lack."""
    days = EVENT_LOG_TTL_DAYS if ttl_days is None else ttl_days
    cutoff = int((now if now is not None else time.time()) - days * 86400)
    deleted = 0
    try:
        while True:
            n = _sweep_batch(db, cutoff)
            deleted += n
            if n < SWEEP_BATCH:
                return deleted
    except Exception as exc:  # noqa: BLE001 — housekeeping must never raise
        _LOG.warning("event_log sweep failed after %d rows: %s", deleted, exc)
        return deleted


async def sweep_event_log_async(db: sqlite3.Connection, *, ttl_days: int | None = None,
                                pause: float = 0.05) -> int:
    """Same as sweep_event_log but yields to the loop between batches, so a
    large backlog never blocks request handling."""
    days = EVENT_LOG_TTL_DAYS if ttl_days is None else ttl_days
    cutoff = int(time.time() - days * 86400)
    deleted = 0
    try:
        while True:
            n = _sweep_batch(db, cutoff)
            deleted += n
            if n < SWEEP_BATCH:
                return deleted
            await asyncio.sleep(pause)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("event_log sweep failed after %d rows: %s", deleted, exc)
        return deleted


async def event_log_sweeper(db: sqlite3.Connection, *, interval: float = 86400.0) -> None:
    """Background task: sweep at startup and then once per interval."""
    while True:
        n = await sweep_event_log_async(db)
        if n:
            _LOG.info("event_log: swept %d rows older than %d days", n, EVENT_LOG_TTL_DAYS)
        await asyncio.sleep(interval)
