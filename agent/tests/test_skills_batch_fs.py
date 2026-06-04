import time, asyncio
import pytest
import db as db_module
from fs.snapshots import SnapshotStore
from skills import filesystem as fsskill


class FakeSink:
    def __init__(self): self.events = []
    async def put(self, e): self.events.append(e)


def _run(coro): return asyncio.get_event_loop().run_until_complete(coro)


def test_batch_fs_in_all_tools():
    names = set()
    for t in fsskill.ALL_TOOLS:
        names.add(getattr(t, "name", None) or getattr(t, "__name__", ""))
    assert "batch_fs" in names


def test_batch_fs_impl_organizes_files(tmp_path):
    conn = db_module.init_db(str(tmp_path / "a.db"),
                             snapshots_root=str(tmp_path / "snap"))
    now = int(time.time())
    conn.execute("INSERT INTO sessions (id,user_id,title,created_at,updated_at) "
                 "VALUES (?,?,?,?,?)", ("s1", "u1", None, now, now))
    root = tmp_path / "root"; root.mkdir(); (root / "a.jpg").write_text("x")
    conn.execute("INSERT INTO visible_resources (session_id,path,kind,added_at) "
                 "VALUES (?,?,?,?)", ("s1", str(root), "folder", now))
    conn.commit()
    fsskill.SESSION_ID_VAR.set("s1"); fsskill.RUN_ID_VAR.set("r1")
    fsskill.EVENT_QUEUE_VAR.set(FakeSink()); fsskill.DB_VAR.set(conn)
    fsskill.STORE_VAR.set(SnapshotStore(root=str(tmp_path / "snap")))
    fsskill.USER_PATTERNS_VAR.set([]); fsskill.CONFIRM_MGR_VAR.set(None)
    ops = [{"op": "mkdir", "path": str(root / "p")},
           {"op": "rename", "path": str(root / "a.jpg"),
            "dst": str(root / "p" / "a.jpg")}]
    out = _run(fsskill._batch_fs_impl(ops))
    assert (root / "p" / "a.jpg").exists()
    assert "暂存" in out or "staged" in out.lower()
