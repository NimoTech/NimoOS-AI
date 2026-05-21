from __future__ import annotations
import sqlite3
from wiki_summary_worker import discovery
from wiki_summary_worker.config import Config


def _make_users_db(path, rows):
    """rows: list of (id, role) tuples."""
    conn = sqlite3.connect(str(path))
    conn.execute("""CREATE TABLE o_users (
        id INTEGER PRIMARY KEY,
        username TEXT, password TEXT, role TEXT, email TEXT,
        nickname TEXT, avatar TEXT, description TEXT,
        created_at TEXT, updated_at TEXT)""")
    for uid, role in rows:
        conn.execute("INSERT INTO o_users (id, username, role) VALUES (?,?,?)",
                     (uid, f"u{uid}", role))
    conn.commit()
    conn.close()


def test_resolve_user_id_uses_config_override(monkeypatch, tmp_path):
    cfg = Config(user_id_header="42")
    monkeypatch.setattr(discovery, "_USERS_DB", tmp_path / "nope.db")
    assert discovery.resolve_user_id(cfg) == "42"


def test_resolve_user_id_picks_admin(monkeypatch, tmp_path):
    db = tmp_path / "user.db"
    _make_users_db(db, [(1, "user"), (2, "admin"), (3, "admin")])
    monkeypatch.setattr(discovery, "_USERS_DB", db)
    assert discovery.resolve_user_id(Config()) == "2"


def test_resolve_user_id_falls_back_to_lowest_id(monkeypatch, tmp_path):
    db = tmp_path / "user.db"
    _make_users_db(db, [(5, "user"), (3, "user"), (9, "user")])
    monkeypatch.setattr(discovery, "_USERS_DB", db)
    assert discovery.resolve_user_id(Config()) == "3"


def test_resolve_user_id_fallback_to_system_when_db_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(discovery, "_USERS_DB", tmp_path / "nope.db")
    assert discovery.resolve_user_id(Config()) == "system"


def test_resolve_user_id_fallback_to_system_when_table_empty(monkeypatch, tmp_path):
    db = tmp_path / "user.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE o_users (id INTEGER PRIMARY KEY, role TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(discovery, "_USERS_DB", db)
    assert discovery.resolve_user_id(Config()) == "system"
