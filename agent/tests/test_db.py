import sqlite3
import tempfile
import os
import pytest
from db import init_db, get_connection

def test_wal_mode(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = init_db(db_path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    conn.close()

def test_tables_created(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = init_db(db_path)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "sessions" in tables
    assert "messages" in tables
    assert "pending_confirmations" in tables
    conn.close()

def test_indexes_created(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = init_db(db_path)
    indexes = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )}
    assert "idx_sessions_user_id" in indexes
    assert "idx_messages_session_id" in indexes
    conn.close()


def test_init_db_publishes_singleton(tmp_path):
    # get_connection() must return the connection produced by the most recent
    # init_db(path=...) — otherwise lazy callers (agent._fetch_attachments,
    # skills.attachments.read_attachment) open a second sqlite at the default
    # _DB_PATH and silently miss rows the service wrote to the real DB.
    import db as db_module
    db_path = str(tmp_path / "singleton.db")
    conn = init_db(db_path)
    assert db_module._conn is conn
    assert get_connection() is conn
    conn.close()
    db_module._conn = None
