import os
import time
import pytest
import db as db_module
from fs import paths as paths_mod


@pytest.fixture
def session(tmp_path):
    conn = db_module.init_db(str(tmp_path / "a.db"),
                             snapshots_root=str(tmp_path / "snap"))
    now = int(time.time())
    conn.execute("INSERT INTO sessions (id, user_id, title, created_at, updated_at) "
                 "VALUES (?,?,?,?,?)", ("s1", "u1", None, now, now))
    conn.commit()
    return conn


def _add_vr(conn, sid, path, kind="folder"):
    conn.execute("INSERT INTO visible_resources (session_id, path, kind, added_at) "
                 "VALUES (?,?,?,?)", (sid, path, kind, int(time.time())))
    conn.commit()


def test_resolve_inside_visible_folder(session, tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "x.txt").write_text("y")
    _add_vr(session, "s1", str(root))
    abs_ = paths_mod.resolve(str(root / "x.txt"), "s1", session)
    assert abs_ == str(root / "x.txt")


def test_resolve_relative_when_single_visible_folder(session, tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    _add_vr(session, "s1", str(root))
    abs_ = paths_mod.resolve("x.txt", "s1", session)
    assert abs_ == str(root / "x.txt")


def test_resolve_relative_rejected_when_multiple_folders(session, tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    _add_vr(session, "s1", str(a))
    _add_vr(session, "s1", str(b))
    with pytest.raises(paths_mod.PermissionDenied):
        paths_mod.resolve("foo.txt", "s1", session)


def test_resolve_outside_scope_rejected(session, tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    _add_vr(session, "s1", str(root))
    with pytest.raises(paths_mod.PermissionDenied):
        paths_mod.resolve(str(other / "x.txt"), "s1", session)


def test_resolve_symlink_escape_rejected(session, tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    target = tmp_path / "secret"
    target.mkdir()
    (target / "k.txt").write_text("k")
    os.symlink(str(target), str(root / "link"))
    _add_vr(session, "s1", str(root))
    with pytest.raises(paths_mod.PermissionDenied):
        paths_mod.resolve(str(root / "link" / "k.txt"), "s1", session)


def test_resolve_null_byte_rejected(session, tmp_path):
    root = tmp_path / "data"; root.mkdir()
    _add_vr(session, "s1", str(root))
    with pytest.raises(paths_mod.PermissionDenied):
        paths_mod.resolve(str(root / "a\x00b"), "s1", session)


def test_resolve_file_kind_only_self(session, tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    f = root / "single.txt"
    f.write_text("x")
    sib = root / "other.txt"
    sib.write_text("y")
    _add_vr(session, "s1", str(f), kind="file")
    # exact file is allowed
    assert paths_mod.resolve(str(f), "s1", session) == str(f)
    # sibling within same dir is NOT allowed because the visible resource is a single file
    with pytest.raises(paths_mod.PermissionDenied):
        paths_mod.resolve(str(sib), "s1", session)
