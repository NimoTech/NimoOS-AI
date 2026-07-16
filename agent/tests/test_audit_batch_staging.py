"""Regression tests for the L4 audit blind spots found in the 2026-07-16
review: batch_fs commit (the system-prompt-preferred multi-file path) and the
staging commit/revert machinery mutated disk with NO audit record, while the
equivalent single-file ops via fs/ops.py were fully audited.
"""
import asyncio
import json
import time

import pytest

import audit as A
import db as db_module
from fs import batch, staging
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
    return asyncio.new_event_loop().run_until_complete(coro)


def _recs(tmp_path):
    p = tmp_path / "audit.log"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines()]


def test_batch_commit_audits_every_op(ctx, tmp_path):
    A.set_audit_path_for_test(str(tmp_path / "audit.log"))
    root = tmp_path / "root"; (root / "a.jpg").write_text("x")
    res = batch.preflight(ctx, [
        {"op": "mkdir", "path": str(root / "p"), "dst": None,
         "parents": False, "recursive": False},
        {"op": "rename", "path": str(root / "a.jpg"),
         "dst": str(root / "p" / "a.jpg"), "parents": False, "recursive": False}])
    assert res.errors == []
    _run(batch.commit(ctx, res))

    fs = [r for r in _recs(tmp_path) if r["event"] == "fs_change"]
    ops = {r["op"] for r in fs}
    assert "mkdir" in ops and "rename" in ops
    assert all(r["session_id"] == "s1" for r in fs)
    assert all("batch_id" in r for r in fs)
    rn = [r for r in fs if r["op"] == "rename"][0]
    assert rn["dst_path"] == str(root / "p" / "a.jpg")


def test_staging_commit_session_audited(ctx, tmp_path):
    A.set_audit_path_for_test(str(tmp_path / "audit.log"))
    root = tmp_path / "root"; (root / "a.jpg").write_text("x")
    res = batch.preflight(ctx, [
        {"op": "mkdir", "path": str(root / "p"), "dst": None,
         "parents": False, "recursive": False}])
    _run(batch.commit(ctx, res))
    staging.commit_session(ctx["conn"], ctx["store"], "s1")

    commits = [r for r in _recs(tmp_path) if r["event"] == "fs_commit"]
    assert commits and commits[-1]["session_id"] == "s1"


def test_staging_revert_audited(ctx, tmp_path):
    A.set_audit_path_for_test(str(tmp_path / "audit.log"))
    root = tmp_path / "root"; (root / "a.jpg").write_text("x")
    res = batch.preflight(ctx, [
        {"op": "mkdir", "path": str(root / "p"), "dst": None,
         "parents": False, "recursive": False}])
    _run(batch.commit(ctx, res))
    out = staging.revert_run(ctx["conn"], ctx["store"], "s1", "r1")
    assert out["status"] == "ok"

    reverts = [r for r in _recs(tmp_path) if r["event"] == "fs_revert"]
    assert reverts and reverts[-1]["op"] == "mkdir"
    assert reverts[-1]["session_id"] == "s1"
