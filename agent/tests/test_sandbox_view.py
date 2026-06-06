import os
import sqlite3
import pytest
from fs import sandbox_view as sv


def _db_with(visible):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE visible_resources (session_id TEXT, path TEXT, kind TEXT)")
    for path, kind in visible:
        conn.execute(
            "INSERT INTO visible_resources VALUES (?,?,?)", ("s1", path, kind))
    conn.commit()
    return conn


def test_folder_robind_mirrors_realpath(tmp_path):
    proj = tmp_path / "proj"
    (proj).mkdir()
    (proj / "a.txt").write_text("hi")
    conn = _db_with([(str(proj), "folder")])
    view = sv.build_view("s1", conn, [])
    real = os.path.realpath(str(proj))
    assert (real, real) in view.ro_binds


def test_blacklisted_dir_and_key_file_masked(tmp_path):
    proj = tmp_path / "proj"; proj.mkdir()
    (proj / ".ssh").mkdir()
    (proj / ".ssh" / "id_rsa").write_text("secret")
    (proj / "deploy.key").write_text("k")
    (proj / "main.py").write_text("print(1)")
    conn = _db_with([(str(proj), "folder")])
    view = sv.build_view("s1", conn, [])
    real = os.path.realpath(str(proj))
    assert os.path.join(real, ".ssh") in view.dir_masks
    assert os.path.join(real, "deploy.key") in view.file_masks
    assert os.path.join(real, "main.py") not in view.file_masks


def test_user_patterns_masked(tmp_path):
    proj = tmp_path / "proj"; proj.mkdir()
    (proj / "secret.env").write_text("x")
    conn = _db_with([(str(proj), "folder")])
    view = sv.build_view("s1", conn, ["*.env"])
    real = os.path.realpath(str(proj))
    assert os.path.join(real, "secret.env") in view.file_masks


def test_fold_when_many_masked_files(tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "FOLD_THRESHOLD", 5)
    proj = tmp_path / "proj"; proj.mkdir()
    d = proj / "keys"; d.mkdir()
    for i in range(10):
        (d / f"k{i}.key").write_text("x")
    conn = _db_with([(str(proj), "folder")])
    view = sv.build_view("s1", conn, [])
    real = os.path.realpath(str(proj))
    assert os.path.join(real, "keys") in view.dir_masks
    assert os.path.join(real, "keys") in view.skipped
    assert not any(p.startswith(os.path.join(real, "keys") + os.sep)
                   for p in view.file_masks)


def test_budget_exhaustion_folds_remaining(tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "MAX_ENTRIES", 3)
    proj = tmp_path / "proj"; proj.mkdir()
    for name in ("d1", "d2", "d3", "d4"):
        sub = proj / name; sub.mkdir()
        (sub / "f").write_text("x")
    conn = _db_with([(str(proj), "folder")])
    view = sv.build_view("s1", conn, [])
    real = os.path.realpath(str(proj))
    assert (real, real) in view.ro_binds
    assert len(view.skipped) >= 1


def test_nested_folders_dedup(tmp_path):
    outer = tmp_path / "o"; (outer / "inner").mkdir(parents=True)
    conn = _db_with([(str(outer), "folder"),
                     (str(outer / "inner"), "folder")])
    view = sv.build_view("s1", conn, [])
    real_outer = os.path.realpath(str(outer))
    real_inner = os.path.realpath(str(outer / "inner"))
    assert (real_outer, real_outer) in view.ro_binds
    assert (real_inner, real_inner) not in view.ro_binds


def test_to_bwrap_args_order_binds_before_masks(tmp_path):
    proj = tmp_path / "proj"; proj.mkdir()
    (proj / "x.key").write_text("k")
    conn = _db_with([(str(proj), "folder")])
    args = sv.to_bwrap_args(sv.build_view("s1", conn, []))
    real = os.path.realpath(str(proj))
    i_bind = args.index(real)
    i_mask = args.index(os.path.join(real, "x.key"))
    assert i_bind < i_mask


def test_exhaustion_folds_unwalked_sibling_secret(tmp_path, monkeypatch):
    # Budget fully scans the root's 2 subdirs + walks "a", then exhausts while
    # entering "b" — "b" (holding a secret) must be folded, never left exposed.
    monkeypatch.setattr(sv, "MAX_ENTRIES", 3)
    proj = tmp_path / "proj"; proj.mkdir()
    a = proj / "a"; a.mkdir(); (a / "ok.txt").write_text("x")
    b = proj / "b"; b.mkdir(); (b / "id_rsa").write_text("SECRET")
    conn = _db_with([(str(proj), "folder")])
    view = sv.build_view("s1", conn, [])
    real = os.path.realpath(str(proj))
    b_dir = os.path.join(real, "b")
    secret = os.path.join(real, "b", "id_rsa")
    # b must be hidden via a fold (dir mask), root fold, or the secret masked —
    # it must NOT be left bound-but-unmasked.
    assert (b_dir in view.dir_masks) or (real in view.dir_masks) or (secret in view.file_masks)
    # And specifically the secret file path must not be silently visible:
    assert not (b_dir not in view.dir_masks and real not in view.dir_masks
                and secret not in view.file_masks)


def test_gitignore_is_not_masked(tmp_path):
    # The module's distinctive behavior: .gitignore'd files are NOT masked
    # (gitignore is noise filtering, not a secret boundary).
    proj = tmp_path / "proj"; proj.mkdir()
    (proj / ".gitignore").write_text("build/\nnotes.txt\n")
    (proj / "build").mkdir(); (proj / "build" / "out.o").write_text("x")
    (proj / "notes.txt").write_text("hello")
    conn = _db_with([(str(proj), "folder")])
    view = sv.build_view("s1", conn, [])
    real = os.path.realpath(str(proj))
    # gitignore'd dir/file must remain visible (not folded, not masked)
    assert os.path.join(real, "build") not in view.dir_masks
    assert os.path.join(real, "notes.txt") not in view.file_masks


def test_single_file_resource_robind(tmp_path):
    f = tmp_path / "doc.txt"; f.write_text("hi")
    conn = _db_with([(str(f), "file")])
    view = sv.build_view("s1", conn, [])
    real = os.path.realpath(str(f))
    assert (real, real) in view.ro_binds


def test_unreadable_dir_is_safe(tmp_path, monkeypatch):
    # os.scandir raising OSError must not crash and must expose nothing extra.
    proj = tmp_path / "proj"; proj.mkdir()
    (proj / "a.txt").write_text("x")
    real = os.path.realpath(str(proj))
    orig_scandir = os.scandir
    def boom(path, *a, **k):
        if os.path.realpath(str(path)) == real:
            raise OSError("permission denied")
        return orig_scandir(path, *a, **k)
    monkeypatch.setattr(os, "scandir", boom)
    conn = _db_with([(str(proj), "folder")])
    view = sv.build_view("s1", conn, [])
    # root still bound; no masks/crash
    assert (real, real) in view.ro_binds
    assert view.file_masks == [] and view.dir_masks == []


def test_authorized_file_matching_user_pattern_not_bound(tmp_path):
    f = tmp_path / "data.csv"; f.write_text("x")
    conn = _db_with([(str(f), "file")])
    view = sv.build_view("s1", conn, ["*.csv"])
    real = os.path.realpath(str(f))
    assert (real, real) not in view.ro_binds   # blacklist drift → not exposed
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
    # since the root isn't bound, no child masks were emitted for it either
    assert not any(p.startswith(real + os.sep) for p in view.file_masks)
