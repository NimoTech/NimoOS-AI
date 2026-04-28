import sqlite3
from pathlib import Path

_DB_PATH = Path(__file__).parent / "agent.db"

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    title       TEXT,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);

-- Pending confirmations are transient: they only have meaning while the
-- agent process holds an in-memory asyncio.Event for them. Rows that survive
-- a restart are useless. The init step below drops the table so old schemas
-- (which keyed by session_id) get rebuilt with the new confirm_id PK.
CREATE TABLE IF NOT EXISTS pending_confirmations (
    confirm_id  TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    action      TEXT NOT NULL,
    description TEXT NOT NULL,
    command     TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);

-- One row per agent turn. Lets reconnecting clients find the latest run
-- and decide whether it's still in progress.
CREATE TABLE IF NOT EXISTS agent_runs (
    id            TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    status        TEXT NOT NULL,   -- running | done | error
    user_message  TEXT,
    error         TEXT,
    created_at    INTEGER NOT NULL,
    finished_at   INTEGER
);

-- Append-only log of SSE events for a run, in emission order.
CREATE TABLE IF NOT EXISTS event_log (
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (run_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_pending_session_id ON pending_confirmations(session_id);
CREATE INDEX IF NOT EXISTS idx_runs_session ON agent_runs(session_id, created_at DESC);
"""

_CRASHED_ERROR_PAYLOAD = (
    '{"type": "error", "content": "agent process restarted; this run was interrupted"}'
)
_CRASHED_DONE_PAYLOAD = '{"type": "done"}'


def init_db(path: str | None = None) -> sqlite3.Connection:
    import time
    db_path = path or str(_DB_PATH)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Drop the transient pending_confirmations table on every startup. Rows
    # there are tied to in-memory asyncio.Events that don't survive a restart;
    # this also handles the schema migration from the old session_id PK shape.
    conn.execute("DROP TABLE IF EXISTS pending_confirmations")
    conn.executescript(_SCHEMA)
    conn.execute("PRAGMA foreign_keys=ON")

    # Any run still flagged 'running' on startup means the prior process died
    # mid-run. Rewrite the state to 'error' and append synthetic error+done
    # events so reconnecting clients see a clean termination instead of
    # waiting forever on a stream that will never produce more events.
    now = int(time.time())
    crashed = conn.execute(
        "SELECT id FROM agent_runs WHERE status='running'"
    ).fetchall()
    for row in crashed:
        run_id = row["id"]
        max_seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM event_log WHERE run_id=?",
            (run_id,),
        ).fetchone()
        next_seq = (max_seq_row["m"] if max_seq_row else 0) + 1
        conn.execute(
            "INSERT INTO event_log (run_id, seq, payload, created_at) VALUES (?,?,?,?)",
            (run_id, next_seq, _CRASHED_ERROR_PAYLOAD, now),
        )
        conn.execute(
            "INSERT INTO event_log (run_id, seq, payload, created_at) VALUES (?,?,?,?)",
            (run_id, next_seq + 1, _CRASHED_DONE_PAYLOAD, now),
        )
        conn.execute(
            "UPDATE agent_runs SET status='error', error='agent_restarted', finished_at=? WHERE id=?",
            (now, run_id),
        )
    conn.commit()
    return conn

_conn: sqlite3.Connection | None = None

def get_connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = init_db()
    return _conn
