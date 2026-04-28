import os
import time
import pytest
import db as db_module


def test_visible_resources_table_created(tmp_path):
    conn = db_module.init_db(str(tmp_path / "a.db"))
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "visible_resources" in tables
    cols = [r[1] for r in conn.execute("PRAGMA table_info(visible_resources)")]
    for c in ("id", "session_id", "path", "kind", "added_at"):
        assert c in cols
    conn.close()


def test_staged_changes_table_created(tmp_path):
    conn = db_module.init_db(str(tmp_path / "a.db"))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(staged_changes)")]
    for c in ("id", "session_id", "run_id", "seq", "op", "path", "dst_path",
              "snapshot_path", "snapshot_kind", "original_uid", "original_gid",
              "original_mode", "size_bytes", "status", "created_at"):
        assert c in cols
    conn.close()


def test_visible_resources_cascades_on_session_delete(tmp_path):
    conn = db_module.init_db(str(tmp_path / "a.db"))
    now = int(time.time())
    conn.execute("INSERT INTO sessions (id, user_id, title, created_at, updated_at) "
                 "VALUES (?,?,?,?,?)", ("s1", "u1", None, now, now))
    conn.execute("INSERT INTO visible_resources (session_id, path, kind, added_at) "
                 "VALUES (?,?,?,?)", ("s1", "/DATA/foo", "folder", now))
    conn.commit()
    conn.execute("DELETE FROM sessions WHERE id=?", ("s1",))
    conn.commit()
    rows = list(conn.execute("SELECT * FROM visible_resources WHERE session_id=?",
                             ("s1",)))
    assert rows == []
    conn.close()


def test_staged_changes_orphan_marked_when_snapshot_missing(tmp_path):
    db_path = str(tmp_path / "a.db")
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir()
    conn = db_module.init_db(db_path, snapshots_root=str(snap_dir))
    now = int(time.time())
    conn.execute("INSERT INTO sessions (id, user_id, title, created_at, updated_at) "
                 "VALUES (?,?,?,?,?)", ("s1", "u1", None, now, now))
    # Pending row whose snapshot file does not exist
    missing = str(snap_dir / "missing.bin")
    conn.execute("INSERT INTO staged_changes "
                 "(session_id, run_id, seq, op, path, snapshot_path, snapshot_kind, "
                 " status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 ("s1", "r1", 1, "write", "/DATA/x", missing, "file",
                  "pending", now))
    conn.commit()
    conn.close()
    # Re-init: reconciliation should mark it orphan
    conn2 = db_module.init_db(db_path, snapshots_root=str(snap_dir))
    row = conn2.execute("SELECT status FROM staged_changes WHERE run_id=?",
                        ("r1",)).fetchone()
    assert row["status"] == "orphan"
    conn2.close()


def test_orphan_session_sidecar_dir_pruned(tmp_path):
    db_path = str(tmp_path / "a.db")
    snap_root = tmp_path / "snapshots"
    snap_root.mkdir()
    # Sidecar dir for session that was never inserted
    (snap_root / "ghost-session").mkdir()
    (snap_root / "ghost-session" / "f.bin").write_bytes(b"x")
    db_module.init_db(db_path, snapshots_root=str(snap_root))
    assert not (snap_root / "ghost-session").exists()
