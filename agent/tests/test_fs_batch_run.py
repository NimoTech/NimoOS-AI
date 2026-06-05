import time, asyncio
import pytest
import db as db_module
from fs import batch
from fs.snapshots import SnapshotStore


class FakeSink:
    def __init__(self): self.events = []
    async def put(self, e): self.events.append(e)


@pytest.fixture
def ctx(tmp_path):
    conn = db_module.init_db(str(tmp_path / "a.db"),
                             snapshots_root=str(tmp_path / "snap"))
    now = int(time.time())
    conn.execute("INSERT INTO sessions (id,user_id,title,created_at,updated_at) "
                 "VALUES (?,?,?,?,?)", ("s1", "u1", None, now, now))
    root = tmp_path / "root"; root.mkdir()
    conn.execute("INSERT INTO visible_resources (session_id,path,kind,added_at) "
                 "VALUES (?,?,?,?)", ("s1", str(root), "folder", now))
    conn.commit()
    return {"conn": conn, "sink": FakeSink(),
            "store": SnapshotStore(root=str(tmp_path / "snap")),
            "session_id": "s1", "run_id": "r1", "user_patterns": [],
            "confirm_mgr": None, "root": str(root)}


def _run(coro): return asyncio.get_event_loop().run_until_complete(coro)


def test_run_batch_blocked_aborts_with_zero_writes(ctx, tmp_path):
    root = tmp_path / "root"; (root / "a.jpg").write_text("x")
    ops = [{"op": "delete", "path": "/etc/passwd", "dst": None,
            "parents": False, "recursive": False},
           {"op": "rename", "path": str(root / "a.jpg"),
            "dst": str(root / "b.jpg"), "parents": False, "recursive": False}]
    out = _run(batch.run_batch(ctx, ops))
    assert "受保护" in out
    assert (root / "a.jpg").exists()
    assert not (root / "b.jpg").exists()
    assert ctx["conn"].execute(
        "SELECT COUNT(*) c FROM staged_changes").fetchone()["c"] == 0


def test_run_batch_errors_aborts(ctx, tmp_path):
    root = tmp_path / "root"
    ops = [{"op": "rename", "path": str(root / "missing"),
            "dst": str(root / "x"), "parents": False, "recursive": False}]
    out = _run(batch.run_batch(ctx, ops))
    assert "未做任何改动" in out or "失败" in out
    assert ctx["conn"].execute(
        "SELECT COUNT(*) c FROM staged_changes").fetchone()["c"] == 0


def test_run_batch_happy_path_commits(ctx, tmp_path):
    root = tmp_path / "root"; (root / "a.jpg").write_text("x")
    ops = [{"op": "mkdir", "path": str(root / "p"), "dst": None,
            "parents": False, "recursive": False},
           {"op": "rename", "path": str(root / "a.jpg"),
            "dst": str(root / "p" / "a.jpg"), "parents": False, "recursive": False}]
    out = _run(batch.run_batch(ctx, ops))
    assert (root / "p" / "a.jpg").exists()
    assert "暂存" in out or "staged" in out.lower()
    assert ctx["conn"].execute(
        "SELECT COUNT(*) c FROM staged_changes").fetchone()["c"] == 2


def test_run_batch_rejects_symlink_target(ctx, tmp_path):
    import os
    root = tmp_path / "root"
    realdir = root / "real"; realdir.mkdir(); (realdir / "f").write_text("x")
    link = root / "link"; os.symlink(str(realdir), str(link))
    out = _run(batch.run_batch(ctx, [
        {"op": "delete", "path": str(link), "dst": None,
         "parents": False, "recursive": True}]))
    assert "符号链接" in out
    assert os.path.islink(str(link))    # untouched
    assert ctx["conn"].execute(
        "SELECT COUNT(*) c FROM staged_changes").fetchone()["c"] == 0


def test_run_batch_mixed_mkdir_move_delete(ctx, tmp_path):
    root = tmp_path / "root"
    (root / "a.jpg").write_text("1"); (root / "b.jpg").write_text("2")
    (root / "junk").mkdir(); (root / "junk" / "x").write_text("9")
    ops = [{"op": "mkdir", "path": str(root / "pics"), "dst": None,
            "parents": False, "recursive": False},
           {"op": "rename", "path": str(root / "a.jpg"),
            "dst": str(root / "pics" / "a.jpg"), "parents": False, "recursive": False},
           {"op": "rename", "path": str(root / "b.jpg"),
            "dst": str(root / "pics" / "b.jpg"), "parents": False, "recursive": False},
           {"op": "delete", "path": str(root / "junk"), "dst": None,
            "parents": False, "recursive": True}]
    out = _run(batch.run_batch(ctx, ops))
    assert (root / "pics" / "a.jpg").exists()
    assert (root / "pics" / "b.jpg").exists()
    assert not (root / "junk").exists()
    rows = ctx["conn"].execute("SELECT batch_id FROM staged_changes").fetchall()
    assert len(rows) == 4
    assert len({r["batch_id"] for r in rows}) == 1   # all one batch
