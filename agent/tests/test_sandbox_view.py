import os
import sqlite3
import pytest
from fs import sandbox_view as sv


def _db_with(visible):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE visible_resources (session_id TEXT, path TEXT, kind TEXT)")
    for path, kind in visible:
        conn.execute("INSERT INTO visible_resources VALUES (?,?,?)", ("s1", path, kind))
    conn.commit()
    return conn


def test_folder_robind_mirrors_realpath(tmp_path):
    proj = tmp_path / "proj"; proj.mkdir()
    (proj / "a.txt").write_text("hi")
    conn = _db_with([(str(proj), "folder")])
    view = sv.build_view("s1", conn, [])
    real = os.path.realpath(str(proj))
    assert (real, real) in view.ro_binds


def test_single_file_resource_robind(tmp_path):
    f = tmp_path / "doc.txt"; f.write_text("hi")
    conn = _db_with([(str(f), "file")])
    view = sv.build_view("s1", conn, [])
    real = os.path.realpath(str(f))
    assert (real, real) in view.ro_binds


def test_nested_folders_dedup(tmp_path):
    outer = tmp_path / "o"; (outer / "inner").mkdir(parents=True)
    conn = _db_with([(str(outer), "folder"), (str(outer / "inner"), "folder")])
    view = sv.build_view("s1", conn, [])
    real_outer = os.path.realpath(str(outer))
    real_inner = os.path.realpath(str(outer / "inner"))
    assert (real_outer, real_outer) in view.ro_binds
    assert (real_inner, real_inner) not in view.ro_binds


def test_large_folder_fully_mounted_no_masks(tmp_path):
    proj = tmp_path / "DATA"; proj.mkdir()
    big = proj / ".system_data"; big.mkdir()
    for i in range(50):
        (big / f"f{i}").write_text("x")
    (proj / "Downloads").mkdir(); (proj / "Downloads" / "a.txt").write_text("hi")
    (proj / "secret.key").write_text("PRIVATE")
    (proj / ".ssh").mkdir(); (proj / ".ssh" / "id_rsa").write_text("KEY")
    conn = _db_with([(str(proj), "folder")])
    view = sv.build_view("s1", conn, [])
    real = os.path.realpath(str(proj))
    assert view.ro_binds == [(real, real)]
    assert view.skipped == []
    assert not hasattr(view, "dir_masks")
    assert not hasattr(view, "file_masks")


def test_authorized_file_matching_user_pattern_not_bound(tmp_path):
    f = tmp_path / "data.csv"; f.write_text("x")
    conn = _db_with([(str(f), "file")])
    view = sv.build_view("s1", conn, ["*.csv"])
    real = os.path.realpath(str(f))
    assert (real, real) not in view.ro_binds
    assert real in view.skipped


def test_authorized_file_no_pattern_still_bound(tmp_path):
    f = tmp_path / "data.csv"; f.write_text("x")
    conn = _db_with([(str(f), "file")])
    view = sv.build_view("s1", conn, [])
    real = os.path.realpath(str(f))
    assert (real, real) in view.ro_binds


def test_authorized_folder_root_matching_user_pattern_not_bound(tmp_path):
    secret = tmp_path / "secretdir"; secret.mkdir()
    (secret / "f.txt").write_text("x")
    conn = _db_with([(str(secret), "folder")])
    view = sv.build_view("s1", conn, ["secretdir/"])
    real = os.path.realpath(str(secret))
    assert (real, real) not in view.ro_binds
    assert real in view.skipped


def test_to_bwrap_args_only_robinds(tmp_path):
    proj = tmp_path / "proj"; proj.mkdir()
    conn = _db_with([(str(proj), "folder")])
    args = sv.to_bwrap_args(sv.build_view("s1", conn, []))
    real = os.path.realpath(str(proj))
    assert args == ["--ro-bind", real, real]
    assert "--tmpfs" not in args
    assert "/dev/null" not in args


def test_folder_root_matching_builtin_blacklist_not_bound(tmp_path):
    # A folder whose own name hits the built-in hard blacklist (.ssh/) must not
    # be mounted, even with no user patterns — the retained per-resource gate.
    ssh = tmp_path / ".ssh"; ssh.mkdir()
    (ssh / "id_rsa").write_text("KEY")
    conn = _db_with([(str(ssh), "folder")])
    view = sv.build_view("s1", conn, [])
    real = os.path.realpath(str(ssh))
    assert (real, real) not in view.ro_binds
    assert real in view.skipped
