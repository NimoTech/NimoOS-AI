import time
import pytest
import db as db_module
from fs import validators


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
    return {"conn": conn, "session_id": "s1", "user_patterns": [], "root": str(root)}


def test_resolve_in_scope_ok(ctx, tmp_path):
    p = tmp_path / "root" / "f.txt"
    assert validators.classify(ctx, str(p)) == ("ok", str(p))


def test_resolve_out_of_scope_need_grant(ctx, tmp_path):
    outside = tmp_path / "outside" / "f.txt"
    kind, _ = validators.classify(ctx, str(outside))
    assert kind == "need_grant"


def test_hard_blacklist_blocked(ctx):
    kind, _ = validators.classify(ctx, "/etc/passwd")
    assert kind == "blocked"
