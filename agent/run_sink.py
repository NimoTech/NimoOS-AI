"""RunSink — per-run event log + pubsub.

The agent run is detached from any single HTTP connection. Events flow from
the agent task into a RunSink, which:

  1. Persists each event to `event_log` so reconnecting clients can replay.
  2. Fans the event out to every live subscriber (multiple browser tabs,
     reconnect after disconnect, etc.).

Drop-in for asyncio.Queue: exposes async `put(event)`, so existing skill code
that does `await queue.put({...})` works unchanged.
"""

import asyncio
import json
import sqlite3
import time


_TERMINAL_TYPES = ("done",)


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

    async def put(self, event: dict) -> None:
        """Persist + fan-out a single event."""
        self._seq += 1
        seq = self._seq
        try:
            self._db.execute(
                "INSERT INTO event_log (run_id, seq, payload, created_at) VALUES (?,?,?,?)",
                (self.run_id, seq, json.dumps(event), int(time.time())),
            )
            self._db.commit()
        except Exception:
            # Persistence is best-effort — we don't want a transient SQLite
            # error to drop an event the user is watching live.
            pass

        self._past.append(event)
        if event.get("type") in _TERMINAL_TYPES:
            self._done = True

        for sub in list(self._subscribers):
            await sub.put(event)

    def subscribe(self) -> tuple[list[dict], asyncio.Queue]:
        """Atomic past+queue. Caller drains past first, then awaits queue.

        The list copy and queue registration happen with no awaits in between,
        so the subscriber cannot miss any event emitted between snapshot and
        registration.
        """
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
