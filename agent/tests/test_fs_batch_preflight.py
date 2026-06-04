import time
import pytest
import db as db_module
from fs import batch


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
    return {"conn": conn, "session_id": "s1", "user_patterns": [],
            "root": str(root)}


def _op(op, path, **kw):
    return {"op": op, "path": path, "dst": kw.get("dst"),
            "parents": kw.get("parents", False),
            "recursive": kw.get("recursive", False)}


def test_preflight_mkdir_then_move_ok(ctx, tmp_path):
    root = tmp_path / "root"
    (root / "a.jpg").write_text("x")
    ops = [_op("mkdir", str(root / "photos")),
           _op("rename", str(root / "a.jpg"), dst=str(root / "photos" / "a.jpg"))]
    res = batch.preflight(ctx, ops)
    assert res.errors == []
    assert len(res.ok) == 2


def test_preflight_circular_move_is_error(ctx, tmp_path):
    root = tmp_path / "root"; (root / "A").mkdir()
    ops = [_op("rename", str(root / "A"), dst=str(root / "A" / "B"))]
    res = batch.preflight(ctx, ops)
    assert len(res.errors) == 1


def test_preflight_blacklist_classified_separately(ctx, tmp_path):
    root = tmp_path / "root"; (root / "ok.txt").write_text("x")
    ops = [_op("delete", "/etc/passwd"),
           _op("delete", str(root / "ok.txt"))]
    res = batch.preflight(ctx, ops)
    assert len(res.blocked) == 1
    assert res.blocked[0]["path"].startswith("/etc")


def test_preflight_huge_recursive_delete_rejected(ctx, tmp_path, monkeypatch):
    root = tmp_path / "root"; big = root / "big"; big.mkdir()
    monkeypatch.setattr(batch, "MAX_DELETE_ENTRIES", 3)
    for i in range(5):
        (big / f"f{i}").write_text("x")
    res = batch.preflight(ctx, [_op("delete", str(big), recursive=True)])
    assert any("过大" in e["reason"] or "too large" in e["reason"].lower()
               for e in res.errors)


def test_preflight_over_max_ops_rejected(ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(batch, "MAX_OPS", 2)
    root = tmp_path / "root"
    ops = [_op("mkdir", str(root / f"d{i}")) for i in range(3)]
    res = batch.preflight(ctx, ops)
    assert len(res.errors) >= 1
