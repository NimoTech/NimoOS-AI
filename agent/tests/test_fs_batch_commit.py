import time
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
            "root": str(root)}


def _run(coro):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro)


def test_commit_applies_in_order_and_tags_batch_id(ctx, tmp_path):
    root = tmp_path / "root"; (root / "a.jpg").write_text("x")
    res = batch.preflight(ctx, [
        {"op": "mkdir", "path": str(root / "p"), "dst": None,
         "parents": False, "recursive": False},
        {"op": "rename", "path": str(root / "a.jpg"),
         "dst": str(root / "p" / "a.jpg"), "parents": False, "recursive": False}])
    assert res.errors == []
    batch_id = _run(batch.commit(ctx, res))
    assert (root / "p" / "a.jpg").exists()
    assert not (root / "a.jpg").exists()
    rows = ctx["conn"].execute(
        "SELECT batch_id FROM staged_changes WHERE session_id='s1'").fetchall()
    assert len(rows) == 2
    assert all(r["batch_id"] == batch_id for r in rows)


def test_commit_emits_single_staged_batch_event(ctx, tmp_path):
    root = tmp_path / "root"; (root / "a.jpg").write_text("x")
    res = batch.preflight(ctx, [
        {"op": "rename", "path": str(root / "a.jpg"),
         "dst": str(root / "b.jpg"), "parents": False, "recursive": False}])
    _run(batch.commit(ctx, res))
    evs = [e for e in ctx["sink"].events if e["type"] == "staged_batch"]
    assert len(evs) == 1
    assert evs[0]["summary"]["rename"] == 1
    assert evs[0]["batch_id"]


def test_commit_emits_event_even_on_midloop_failure(ctx, tmp_path):
    root = tmp_path / "root"; (root / "a.jpg").write_text("x")
    res = batch.preflight(ctx, [
        {"op": "mkdir", "path": str(root / "p"), "dst": None,
         "parents": False, "recursive": False},
        {"op": "rename", "path": str(root / "a.jpg"),
         "dst": str(root / "b.jpg"), "parents": False, "recursive": False}])
    assert res.errors == []
    (root / "b.jpg").write_text("squat")   # collide AFTER preflight
    import pytest
    with pytest.raises(Exception):
        _run(batch.commit(ctx, res))
    evs = [e for e in ctx["sink"].events if e["type"] == "staged_batch"]
    assert len(evs) == 1                       # event still emitted
    assert evs[0]["batch_id"]
    assert evs[0]["summary"]["mkdir"] == 1     # the mkdir that succeeded
    assert evs[0]["summary"]["rename"] == 0


def test_commit_delete_file_snapshots_and_removes(ctx, tmp_path):
    root = tmp_path / "root"; f = root / "junk.txt"; f.write_text("bye")
    res = batch.preflight(ctx, [
        {"op": "delete", "path": str(f), "dst": None,
         "parents": False, "recursive": False}])
    _run(batch.commit(ctx, res))
    assert not f.exists()
    row = ctx["conn"].execute(
        "SELECT op, snapshot_path FROM staged_changes WHERE session_id='s1'"
    ).fetchone()
    assert row["op"] == "delete_file"
    assert row["snapshot_path"]   # snapshot was taken for undo
