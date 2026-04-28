import sqlite3
from pathlib import Path

import db as db_module


def test_sessions_has_thinking_columns(tmp_path):
    db_path = str(tmp_path / "agent.db")
    snaps = str(tmp_path / "snaps")
    conn = db_module.init_db(path=db_path, snapshots_root=snaps)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
    assert "thinking_enabled" in cols
    assert "thinking_level" in cols


def test_user_settings_table_exists(tmp_path):
    db_path = str(tmp_path / "agent.db")
    snaps = str(tmp_path / "snaps")
    conn = db_module.init_db(path=db_path, snapshots_root=snaps)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(user_settings)")}
    assert {"user_id", "key", "value"} <= cols


def test_old_db_migrates_idempotently(tmp_path):
    """An existing DB without thinking columns gets them added on init."""
    db_path = str(tmp_path / "agent.db")
    snaps = str(tmp_path / "snaps")
    pre = sqlite3.connect(db_path)
    pre.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
    """)
    pre.commit()
    pre.close()
    conn = db_module.init_db(path=db_path, snapshots_root=snaps)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
    assert "thinking_enabled" in cols
    assert "thinking_level" in cols
