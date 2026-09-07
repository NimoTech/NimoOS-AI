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
`message_delta` events — and the same inner types wrapped in
`subagent_event` for one `parent_call_id` — are merged into ONE row, flushed
when the type changes, when the buffer exceeds COALESCE_MAX_CHARS, after
FLUSH_INTERVAL seconds, on subscribe(), or on the terminal event. Live
subscribers still receive every raw delta; only the replay log (`_past`,
`event_log`) is coalesced — the UI reducers append deltas, so a merged delta
renders identically.
"""

import asyncio
import json
import logging
import sqlite3
import time

_LOG = logging.getLogger("nimoos-agent.run_sink")

_TERMINAL_TYPES = ("done",)
COALESCE_TYPES = frozenset({"thinking", "message_delta"})
FLUSH_INTERVAL = 0.25          # seconds a pending delta buffer may wait
COALESCE_MAX_CHARS = 4000      # flush when the merged content grows past this
EVENT_LOG_TTL_DAYS = 30        # sweep_event_log(): rows older than this go


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
        # Coalescing state: the delta row being accumulated (a deep-enough copy
        # of the first event), its stream key, and the pending timer handle.
        self._pending: dict | None = None
        self._pending_key = None
        self._flush_handle: asyncio.TimerHandle | None = None

    async def put(self, event: dict) -> None:
        """Fan out immediately; persist coalesced (see module docstring)."""
        key = _coalesce_key(event)
        if key is not None and self._pending is not None and self._pending_key == key \
                and _content_len(self._pending) + _content_len(event) <= COALESCE_MAX_CHARS:
            _merge_into(self._pending, event)
        else:
            self.flush()
            if key is not None:
                self._pending = self._copy_delta(event)
                self._pending_key = key
                self._arm_timer()
            else:
                self._persist(event)
        if event.get("type") in _TERMINAL_TYPES:
            self.flush()
            self._done = True
        for sub in list(self._subscribers):
            await sub.put(event)

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
        """Persist the pending delta buffer (if any). Sync; safe to call often."""
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        if self._pending is None:
            return
        ev, self._pending, self._pending_key = self._pending, None, None
        self._persist(ev)

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


def sweep_event_log(db: sqlite3.Connection, *, ttl_days: int = EVENT_LOG_TTL_DAYS,
                    now: float | None = None) -> int:
    """Delete event_log rows older than ttl_days. Rows are the replay source
    for the run-stream and the task transcript; tasks keep their last 50 runs
    (tasks.store.prune_runs) but nothing ever reclaimed the event rows of
    ordinary chat runs — 1.76 M rows / 326 MB on 118 by 2026-09-07."""
    cutoff = int((now if now is not None else time.time()) - ttl_days * 86400)
    try:
        cur = db.execute("DELETE FROM event_log WHERE created_at < ?", (cutoff,))
        db.commit()
        return int(cur.rowcount or 0)
    except Exception as exc:  # noqa: BLE001 — housekeeping must never raise
        _LOG.warning("event_log sweep failed: %s", exc)
        return 0
