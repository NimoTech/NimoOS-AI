import os
import shutil
import sqlite3
from pathlib import Path

_DB_PATH = Path(__file__).parent / "agent.db"

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS sessions (
    id                TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    title             TEXT,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    thinking_enabled  INTEGER,
    thinking_level    TEXT,
    agent_type        TEXT NOT NULL DEFAULT 'general',
    network_granted   INTEGER NOT NULL DEFAULT 0,
    recall_indexed_msgs INTEGER NOT NULL DEFAULT 0,
    recall_chunk_seq    INTEGER NOT NULL DEFAULT 0,
    rolling_summary       TEXT,
    folded_upto           INTEGER NOT NULL DEFAULT 0,
    last_overhead_tokens  INTEGER NOT NULL DEFAULT 0,
    source            TEXT NOT NULL DEFAULT 'web'
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id     TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (user_id, key)
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

CREATE TABLE IF NOT EXISTS visible_resources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK(kind IN ('folder','file')),
    added_at    INTEGER NOT NULL,
    UNIQUE(session_id, path)
);

CREATE TABLE IF NOT EXISTS staged_changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    run_id          TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    op              TEXT NOT NULL CHECK(op IN
                       ('write','edit','delete_file','delete_dir','mkdir','rename')),
    path            TEXT NOT NULL,
    dst_path        TEXT,
    snapshot_path   TEXT,
    snapshot_kind   TEXT CHECK(snapshot_kind IN ('file','tar') OR snapshot_kind IS NULL),
    original_uid    INTEGER,
    original_gid    INTEGER,
    original_mode   INTEGER,
    size_bytes      INTEGER,
    status          TEXT NOT NULL DEFAULT 'pending'
                     CHECK(status IN ('pending','committed','reverted','orphan')),
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_staged_run ON staged_changes(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_staged_session_pending
  ON staged_changes(session_id, status);
CREATE INDEX IF NOT EXISTS idx_visible_session
  ON visible_resources(session_id);

CREATE TABLE IF NOT EXISTS attachments (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_id   TEXT,
    filename     TEXT NOT NULL,
    mime         TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK(kind IN ('image','text','video','audio','binary','document')),
    size_bytes   INTEGER NOT NULL,
    rel_path     TEXT NOT NULL,
    meta_json    TEXT,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_session ON attachments(session_id);
CREATE INDEX IF NOT EXISTS idx_attachments_msg     ON attachments(message_id);

-- Durable record of each file-access authorization request and its outcome.
-- Unlike pending_confirmations (dropped each startup), this MUST persist so a
-- refreshed page can rebuild the resolved card in conversation history.
CREATE TABLE IF NOT EXISTS access_requests (
    confirm_id  TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    run_id      TEXT NOT NULL,
    path        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    reason      TEXT NOT NULL,
    reason_key  TEXT,
    decision    TEXT,
    created_at  INTEGER NOT NULL,
    resolved_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_access_req_session
  ON access_requests(session_id, created_at);

CREATE TABLE IF NOT EXISTS memory_entries (
    id                TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    kind              TEXT NOT NULL,              -- 'preference' | 'fact' | 'goal'
    text              TEXT NOT NULL,
    source            TEXT NOT NULL,              -- 'auto' | 'tool' | 'user'
    trust             TEXT NOT NULL DEFAULT 'normal', -- 'normal' | 'low'
    priority          INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'active', -- 'active'|'disabled'|'superseded'
    lineage_id        TEXT NOT NULL,
    supersedes        TEXT,
    recall_count      INTEGER NOT NULL DEFAULT 0,
    last_recalled_at  INTEGER NOT NULL,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    expires_at        INTEGER,
    origin_session_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_user_active
    ON memory_entries(user_id, status, priority DESC, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_lineage
    ON memory_entries(lineage_id, created_at);

CREATE TABLE IF NOT EXISTS memory_extract_jobs (
    session_id    TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending', -- 'pending'|'running'|'done'|'error'
    attempts      INTEGER NOT NULL DEFAULT 0,
    provider_url  TEXT,
    provider_key  TEXT,
    provider_type TEXT,
    model_name    TEXT,
    last_error    TEXT,
    enqueued_at   INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_extract_jobs_status
    ON memory_extract_jobs(status, enqueued_at);

CREATE TABLE IF NOT EXISTS recall_index_jobs (
    session_id   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending', -- 'pending'|'running'|'done'|'error'
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    enqueued_at  INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recall_jobs_status
    ON recall_index_jobs(status, enqueued_at);

CREATE TABLE IF NOT EXISTS mcp_tokens (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    label        TEXT NOT NULL DEFAULT '',
    token_hash   TEXT NOT NULL UNIQUE,
    created_at   INTEGER NOT NULL,
    last_used_at INTEGER,
    revoked      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mcp_tokens_user ON mcp_tokens(user_id);

CREATE TABLE IF NOT EXISTS channel_instances (
    id           TEXT PRIMARY KEY,
    channel_type TEXT NOT NULL,
    scope        TEXT NOT NULL DEFAULT 'system',
    name         TEXT,
    config_json  TEXT NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_by   TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_bindings (
    id                TEXT PRIMARY KEY,
    instance_id       TEXT NOT NULL,
    external_user_id  TEXT NOT NULL,
    external_username TEXT,
    user_id           TEXT NOT NULL,
    default_model     TEXT,
    download_dir      TEXT,
    revoked           INTEGER NOT NULL DEFAULT 0,
    created_at        INTEGER NOT NULL,
    UNIQUE(instance_id, external_user_id)
);
CREATE INDEX IF NOT EXISTS idx_channel_bindings_user ON channel_bindings(user_id);

CREATE TABLE IF NOT EXISTS channel_pairing_codes (
    id          TEXT PRIMARY KEY,
    code_hash   TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    expires_at  INTEGER NOT NULL,
    used_at     INTEGER
);

CREATE TABLE IF NOT EXISTS channel_chats (
    id               TEXT PRIMARY KEY,
    instance_id      TEXT NOT NULL,
    external_chat_id TEXT NOT NULL,
    binding_id       TEXT NOT NULL,
    session_id       TEXT NOT NULL,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    UNIQUE(instance_id, external_chat_id)
);

CREATE TABLE IF NOT EXISTS shell_allowlist (
    id          TEXT PRIMARY KEY,
    match_type  TEXT NOT NULL CHECK(match_type IN ('prefix','regex','path_scope')),
    value       TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL
);
"""

_DEFAULT_SNAPSHOTS_ROOT = "/var/lib/nimoos/ai/agent/snapshots"

_CRASHED_ERROR_PAYLOAD = (
    '{"type": "error", "content": "agent process restarted; this run was interrupted"}'
)
_CRASHED_DONE_PAYLOAD = '{"type": "done"}'


def init_db(path: str | None = None, snapshots_root: str | None = None) -> sqlite3.Connection:
    import time
    global _conn
    db_path = path or str(_DB_PATH)
    snaps = snapshots_root or _DEFAULT_SNAPSHOTS_ROOT
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Drop the transient pending_confirmations table on every startup. Rows
    # there are tied to in-memory asyncio.Events that don't survive a restart;
    # this also handles the schema migration from the old session_id PK shape.
    conn.execute("DROP TABLE IF EXISTS pending_confirmations")
    conn.executescript(_SCHEMA)
    # Migration: rebuild attachments table if its CHECK constraint predates
    # kind='document'. Idempotent — runs at most once per DB.
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='attachments'"
    ).fetchone()
    if row and row[0] and "'document'" not in row[0]:
        conn.executescript("""
        CREATE TABLE attachments_new (
            id           TEXT PRIMARY KEY,
            session_id   TEXT NOT NULL,
            message_id   TEXT,
            filename     TEXT NOT NULL,
            mime         TEXT NOT NULL,
            kind         TEXT NOT NULL CHECK(kind IN ('image','text','video','audio','binary','document')),
            size_bytes   INTEGER NOT NULL,
            rel_path     TEXT NOT NULL,
            meta_json    TEXT,
            created_at   INTEGER NOT NULL,
            FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
        INSERT INTO attachments_new
            SELECT id, session_id, message_id, filename, mime, kind,
                   size_bytes, rel_path, meta_json, created_at
            FROM attachments;
        DROP TABLE attachments;
        ALTER TABLE attachments_new RENAME TO attachments;
        CREATE INDEX IF NOT EXISTS idx_attachments_session ON attachments(session_id);
        CREATE INDEX IF NOT EXISTS idx_attachments_msg     ON attachments(message_id);
        """)
    # Idempotent ALTER for existing databases without thinking columns.
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "thinking_enabled" not in existing:
        conn.execute("ALTER TABLE sessions ADD COLUMN thinking_enabled INTEGER")
    if "thinking_level" not in existing:
        conn.execute("ALTER TABLE sessions ADD COLUMN thinking_level TEXT")
    if "agent_type" not in existing:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN agent_type TEXT NOT NULL DEFAULT 'general'")
    if "network_granted" not in existing:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN network_granted INTEGER NOT NULL DEFAULT 0")
    if "unlocked_tool_categories" not in existing:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN unlocked_tool_categories TEXT")
    if "recall_indexed_msgs" not in existing:
        conn.execute("ALTER TABLE sessions ADD COLUMN "
                     "recall_indexed_msgs INTEGER NOT NULL DEFAULT 0")
    if "recall_chunk_seq" not in existing:
        conn.execute("ALTER TABLE sessions ADD COLUMN "
                     "recall_chunk_seq INTEGER NOT NULL DEFAULT 0")
    if "rolling_summary" not in existing:
        conn.execute("ALTER TABLE sessions ADD COLUMN rolling_summary TEXT")
    if "folded_upto" not in existing:
        conn.execute("ALTER TABLE sessions ADD COLUMN "
                     "folded_upto INTEGER NOT NULL DEFAULT 0")
    if "last_overhead_tokens" not in existing:
        conn.execute("ALTER TABLE sessions ADD COLUMN "
                     "last_overhead_tokens INTEGER NOT NULL DEFAULT 0")
    if "source" not in existing:
        conn.execute("ALTER TABLE sessions ADD COLUMN "
                     "source TEXT NOT NULL DEFAULT 'web'")
    # Idempotent ALTER for existing databases without the reason_key column.
    ar_cols = {row["name"] for row in conn.execute("PRAGMA table_info(access_requests)")}
    if ar_cols and "reason_key" not in ar_cols:
        conn.execute("ALTER TABLE access_requests ADD COLUMN reason_key TEXT")
    # Idempotent ALTER for existing databases without batch_id column.
    staged_cols = {row["name"]
                   for row in conn.execute("PRAGMA table_info(staged_changes)")}
    if "batch_id" not in staged_cols:
        conn.execute("ALTER TABLE staged_changes ADD COLUMN batch_id TEXT")
    # Idempotent ALTER for existing databases without download_dir column.
    cb_cols = {r["name"] for r in conn.execute("PRAGMA table_info(channel_bindings)")}
    if "download_dir" not in cb_cols:
        conn.execute("ALTER TABLE channel_bindings ADD COLUMN download_dir TEXT")
    # Idempotent ALTER for existing databases without the trust column.
    mem_cols = {r["name"] for r in conn.execute("PRAGMA table_info(memory_entries)")}
    if "trust" not in mem_cols:
        conn.execute("ALTER TABLE memory_entries ADD COLUMN "
                     "trust TEXT NOT NULL DEFAULT 'normal'")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_staged_batch "
        "ON staged_changes(session_id, batch_id)")
    conn.commit()
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

    # NEW: staged_changes orphan detection — pending rows whose snapshot files
    # no longer exist on disk are marked orphan so they cannot be reverted.
    pending = conn.execute(
        "SELECT id, snapshot_path, op FROM staged_changes WHERE status='pending'"
    ).fetchall()
    for row in pending:
        sp = row["snapshot_path"]
        op = row["op"]
        # Ops that do not require a snapshot: mkdir, rename
        if op in ("mkdir", "rename"):
            continue
        if sp and not os.path.exists(sp):
            conn.execute("UPDATE staged_changes SET status='orphan' WHERE id=?",
                         (row["id"],))

    # NEW: prune ghost sidecar dirs (session dirs whose session_id is no longer
    # in the sessions table — left over from a deleted or never-committed session).
    if os.path.isdir(snaps):
        live = {r["id"] for r in conn.execute("SELECT id FROM sessions")}
        for entry in os.listdir(snaps):
            full = os.path.join(snaps, entry)
            if not os.path.isdir(full):
                continue
            if entry not in live:
                shutil.rmtree(full, ignore_errors=True)

    conn.commit()
    # Publish the just-initialized connection as the module singleton so
    # `get_connection()` returns this same handle. Without this, callers that
    # lazy-fetch via `get_connection()` (agent._fetch_attachments,
    # skills.attachments.read_attachment) end up opening a SECOND sqlite file
    # at the default `_DB_PATH` (next to db.py), bypassing whatever path the
    # service started with — attachments uploaded against the real DB become
    # invisible to the agent run loop.
    _conn = conn
    return conn

_conn: sqlite3.Connection | None = None

def get_connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = init_db()
    return _conn


def is_network_granted(conn, session_id: str) -> bool:
    row = conn.execute(
        "SELECT network_granted FROM sessions WHERE id=?", (session_id,)
    ).fetchone()
    return bool(row and row["network_granted"])


def grant_network(conn, session_id: str) -> None:
    conn.execute(
        "UPDATE sessions SET network_granted=1 WHERE id=?", (session_id,))
    conn.commit()


import json as _json


def get_unlocked_categories(session_id: str, conn=None) -> list[str]:
    """返回该会话已解锁的工具类别(NULL/缺失→[])。"""
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT unlocked_tool_categories FROM sessions WHERE id=?",
        (session_id,),
    ).fetchone()
    if not row or row[0] is None:
        return []
    try:
        val = _json.loads(row[0])
        return val if isinstance(val, list) else []
    except (ValueError, TypeError):
        return []


def set_unlocked_categories(session_id: str, categories: list[str], conn=None) -> None:
    """覆盖写该会话的已解锁类别(去重排序后存 JSON 数组)。"""
    conn = conn or get_connection()
    payload = _json.dumps(sorted(set(categories)))
    conn.execute(
        "UPDATE sessions SET unlocked_tool_categories=? WHERE id=?",
        (payload, session_id),
    )
    conn.commit()
