import os
import time
import pytest
import db as db_module
from fs import staging
from fs.snapshots import SnapshotStore


@pytest.fixture
def env(tmp_path):
    conn = db_module.init_db(str(tmp_path / "a.db"),
                             snapshots_root=str(tmp_path / "snap"))
    conn.execute("INSERT INTO sessions (id, user_id, title, created_at, updated_at) "
                 "VALUES (?,?,?,?,?)", ("s1", "u1", None, 0, 0))
    conn.commit()
    store = SnapshotStore(root=str(tmp_path / "snap"))
    return conn, store, tmp_path


def _record_write(conn, store, path, content="orig", run_id="r1", seq=1):
    p = path
    p.write_text(content)
    snap = store.take_file("s1", run_id, str(seq), str(p))
    return staging.record(conn, "s1", run_id, seq, "write", str(p),
                           snapshot_path=snap, snapshot_kind="file",
                           original_uid=os.stat(p).st_uid,
                           original_gid=os.stat(p).st_gid,
                           original_mode=os.stat(p).st_mode & 0o777,
                           size_bytes=len(content))


def test_record_inserts_pending_row(env):
    conn, store, tp = env
    f = tp / "a.txt"
    _record_write(conn, store, f)
    row = conn.execute("SELECT status, op FROM staged_changes "
                       "WHERE session_id='s1' AND run_id='r1'").fetchone()
    assert row["status"] == "pending"
    assert row["op"] == "write"


def test_take_file_skipped_for_same_path_same_run(env):
    conn, store, tp = env
    f = tp / "a.txt"; f.write_text("orig")
    # First snapshot
    snap1 = staging.maybe_take_file_snapshot(conn, store, "s1", "r1", "1", str(f))
    # Insert a record so the next call sees an existing snapshot for the path
    staging.record(conn, "s1", "r1", 1, "write", str(f),
                   snapshot_path=snap1, snapshot_kind="file",
                   original_uid=os.stat(f).st_uid,
                   original_gid=os.stat(f).st_gid,
                   original_mode=os.stat(f).st_mode & 0o777,
                   size_bytes=4)
    # Second op on same path same run: helper returns previous snapshot path
    f.write_text("change-1")
    snap2 = staging.maybe_take_file_snapshot(conn, store, "s1", "r1", "2", str(f))
    assert snap1 == snap2
    # Restore should reproduce the *original* content
    store.restore_file(snap1, str(f))
    assert f.read_text() == "orig"


def test_revert_run_restores_files_in_reverse(env):
    conn, store, tp = env
    f = tp / "a.txt"
    f.write_text("original")
    snap = store.take_file("s1", "r1", "1", str(f))
    staging.record(conn, "s1", "r1", 1, "write", str(f),
                   snapshot_path=snap, snapshot_kind="file",
                   original_uid=os.stat(f).st_uid,
                   original_gid=os.stat(f).st_gid,
                   original_mode=os.stat(f).st_mode & 0o777,
                   size_bytes=8)
    # Mutate file as if agent wrote it
    f.write_text("mutated")
    res = staging.revert_run(conn, store, "s1", "r1")
    assert res["status"] == "ok"
    assert f.read_text() == "original"


def test_revert_run_rejects_when_snapshot_missing(env):
    conn, store, tp = env
    f = tp / "a.txt"; f.write_text("orig")
    snap = store.take_file("s1", "r1", "1", str(f))
    staging.record(conn, "s1", "r1", 1, "write", str(f),
                   snapshot_path=snap, snapshot_kind="file",
                   original_uid=os.stat(f).st_uid,
                   original_gid=os.stat(f).st_gid,
                   original_mode=os.stat(f).st_mode & 0o777,
                   size_bytes=4)
    os.remove(snap)
    res = staging.revert_run(conn, store, "s1", "r1")
    assert res["status"] == "snapshot_missing"


def test_commit_session_drops_sidecar_and_marks_committed(env):
    conn, store, tp = env
    f = tp / "a.txt"; _record_write(conn, store, f)
    staging.commit_session(conn, store, "s1")
    statuses = [r["status"] for r in conn.execute(
        "SELECT status FROM staged_changes WHERE session_id='s1'")]
    assert all(s == "committed" for s in statuses)
    assert not os.path.exists(os.path.join(str(tp / "snap"), "s1"))


def test_revert_partial_returns_207(env):
    conn, store, tp = env
    f1 = tp / "a.txt"; f1.write_text("a")
    f2 = tp / "b.txt"; f2.write_text("b")
    snap1 = store.take_file("s1", "r1", "1", str(f1))
    snap2 = store.take_file("s1", "r1", "2", str(f2))
    for path, snap, seq in [(f1, snap1, 1), (f2, snap2, 2)]:
        staging.record(conn, "s1", "r1", seq, "write", str(path),
                       snapshot_path=snap, snapshot_kind="file",
                       original_uid=os.stat(path).st_uid,
                       original_gid=os.stat(path).st_gid,
                       original_mode=os.stat(path).st_mode & 0o777,
                       size_bytes=1)
    # Make f1 unwritable so revert fails on it (high seq processed first)
    f1.write_text("after")
    f2.write_text("after")
    os.chmod(f1, 0o400)
    try:
        res = staging.revert_run(conn, store, "s1", "r1")
        # We expect partial — at least one ok, at least one failed
        assert res["status"] == "partial"
        assert res["failed"]
    finally:
        os.chmod(f1, 0o600)


def test_record_persists_batch_id(env, tmp_path):
    conn, store, tp = env
    f = tp / "a.txt"; f.write_text("x")
    staging.record(conn, "s1", "r1", 1, "mkdir", str(f), batch_id="b-123")
    row = conn.execute(
        "SELECT batch_id FROM staged_changes WHERE session_id='s1'").fetchone()
    assert row["batch_id"] == "b-123"
