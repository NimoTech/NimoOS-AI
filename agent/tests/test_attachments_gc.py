import os
import sqlite3
from pathlib import Path
import db as db_module
from attachments.gc import run_startup_gc


_NOW = 1_700_000_000


def _setup(tmp_path):
    db_path = str(tmp_path / "agent.db")
    db_module.init_db(db_path, snapshots_root=str(tmp_path / "snaps"))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    root = tmp_path / "data"
    (root / "sessions").mkdir(parents=True)
    return conn, root


def _mk_session(conn, sid: str):
    conn.execute(
        "INSERT INTO sessions (id,user_id,created_at,updated_at) VALUES (?,?,0,0)",
        (sid, "u1"))
    conn.commit()


def _mk_attachment(conn, root: Path, *, aid, sid, message_id, age_seconds):
    conn.execute(
        "INSERT INTO attachments "
        "(id,session_id,message_id,filename,mime,kind,size_bytes,rel_path,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (aid, sid, message_id, f"{aid}.txt", "text/plain", "text", 3,
         f"{aid}__x.txt", _NOW - age_seconds))
    conn.commit()
    p = root / "sessions" / sid / "attachments"
    p.mkdir(parents=True, exist_ok=True)
    (p / f"{aid}__x.txt").write_text("abc")


def test_old_draft_attachment_is_deleted(tmp_path):
    conn, root = _setup(tmp_path)
    _mk_session(conn, "s1")
    _mk_attachment(conn, root, aid="a_old", sid="s1",
                   message_id=None, age_seconds=86400 * 2)
    run_startup_gc(conn, str(root), age_seconds=86400, now=_NOW)
    assert conn.execute(
        "SELECT COUNT(*) FROM attachments WHERE id='a_old'"
    ).fetchone()[0] == 0
    assert not (root / "sessions/s1/attachments/a_old__x.txt").exists()


def test_recent_draft_attachment_is_kept(tmp_path):
    conn, root = _setup(tmp_path)
    _mk_session(conn, "s1")
    _mk_attachment(conn, root, aid="a_new", sid="s1",
                   message_id=None, age_seconds=3600)
    run_startup_gc(conn, str(root), age_seconds=86400, now=_NOW)
    assert conn.execute(
        "SELECT COUNT(*) FROM attachments WHERE id='a_new'"
    ).fetchone()[0] == 1


def test_bound_attachment_is_kept_even_if_old(tmp_path):
    conn, root = _setup(tmp_path)
    _mk_session(conn, "s1")
    _mk_attachment(conn, root, aid="a_bound", sid="s1",
                   message_id="m1", age_seconds=86400 * 30)
    run_startup_gc(conn, str(root), age_seconds=86400, now=_NOW)
    assert conn.execute(
        "SELECT COUNT(*) FROM attachments WHERE id='a_bound'"
    ).fetchone()[0] == 1


def test_orphan_session_dir_is_cleaned(tmp_path):
    conn, root = _setup(tmp_path)
    orphan = root / "sessions" / "ghost" / "attachments"
    orphan.mkdir(parents=True)
    (orphan / "junk.bin").write_text("x")
    run_startup_gc(conn, str(root), age_seconds=86400, now=_NOW)
    assert not (root / "sessions/ghost").exists()


def test_gc_idempotent(tmp_path):
    conn, root = _setup(tmp_path)
    run_startup_gc(conn, str(root), age_seconds=86400, now=_NOW)
    run_startup_gc(conn, str(root), age_seconds=86400, now=_NOW)
