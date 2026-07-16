import os
import shell_guard.backstop as B


def test_fs_type_unknown_on_missing(monkeypatch):
    # fs_type walks up to the nearest existing ancestor, so this reports the
    # host's real root filesystem type. Include both "overlay" (docker
    # overlay2 label) and "overlayfs" (coreutils stat's name for the same
    # magic number) since sandboxed/containerized dev environments commonly
    # have an overlayfs root.
    assert B.fs_type("/nonexistent/xyz") in (
        "unknown", "ext2/ext3", "btrfs", "tmpfs", "overlay", "overlayfs",
    )


def test_trash_hardlink_preserves_data_after_delete(tmp_path, monkeypatch):
    # Force the non-btrfs branch
    monkeypatch.setattr(B, "fs_type", lambda p: "ext2/ext3")
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    trash = tmp_path / ".trash"
    res = B.prepare_backstop([str(victim)], trash_root=str(trash))
    assert res.kind == "trash" and res.undoable is True
    # Simulate the destructive command:
    os.remove(victim)
    assert not victim.exists()
    # Data survives in trash via hardlink:
    linked = list(trash.rglob("victim.txt"))
    assert linked and linked[0].read_text() == "precious"


def test_unparseable_no_targets_reports_not_undoable(monkeypatch):
    monkeypatch.setattr(B, "fs_type", lambda p: "ext2/ext3")
    res = B.prepare_backstop([], trash_root="/tmp/ignored")
    assert res.kind == "none" and res.undoable is False


def test_prune_keeps_newest(tmp_path):
    root = tmp_path / "trash"
    root.mkdir()
    for i in range(5):
        (root / f"{1000 + i}").mkdir()
    removed = B.prune(str(root), keep=2)
    assert removed == 3
    assert sorted(p.name for p in root.iterdir()) == ["1003", "1004"]


def test_makedirs_failure_never_raises(tmp_path, monkeypatch):
    # Contract: prepare_backstop must NEVER raise — always return a
    # BackstopResult, even when the trash dir cannot be created.
    monkeypatch.setattr(B, "fs_type", lambda p: "ext2/ext3")

    def raise_oserror(*args, **kwargs):
        raise OSError("boom: no space left on device")

    monkeypatch.setattr(B.os, "makedirs", raise_oserror)
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    res = B.prepare_backstop([str(victim)], trash_root=str(tmp_path / ".trash"))
    assert res.kind == "none"
    assert res.undoable is False


def test_mixed_mount_does_not_take_snapshot_shortcut(tmp_path, monkeypatch):
    # One target on btrfs, one on ext4 → the snapshot shortcut (which would
    # silently skip the non-btrfs target) must NOT be taken; fall through to
    # the per-target hardlink path instead.
    a = tmp_path / "a.txt"
    a.write_text("aaa")
    b = tmp_path / "b.txt"
    b.write_text("bbb")

    def fake_fs_type(p):
        return "btrfs" if p == str(a) else "ext2/ext3"

    monkeypatch.setattr(B, "fs_type", fake_fs_type)
    trash = tmp_path / ".trash"
    res = B.prepare_backstop([str(a), str(b)], trash_root=str(trash))
    assert res.kind != "snapshot"


def test_glob_target_is_expanded_and_backed_up(tmp_path, monkeypatch):
    # 2026-07-16 review: `rm -rf /DATA/Documents/*` reached prepare_backstop
    # with the literal token '/DATA/Documents/*'; os.path.exists('.../*') is
    # False, so NO backup was taken while bash expanded and deleted for real.
    # The backstop must expand globs the same way the shell would.
    monkeypatch.setattr(B, "fs_type", lambda p: "ext2/ext3")
    docs = tmp_path / "Documents"
    docs.mkdir()
    (docs / "a.txt").write_text("A")
    (docs / "b.txt").write_text("B")
    res = B.prepare_backstop([str(docs / "*")], trash_root=str(tmp_path / "trash"))
    assert res.undoable is True
    assert res.kind == "trash"
    # both expanded files were preserved in the trash
    import os
    saved = []
    for root, _dirs, files in os.walk(res.location):
        saved.extend(files)
    assert "a.txt" in saved and "b.txt" in saved


def test_nonmatching_glob_reports_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "fs_type", lambda p: "ext2/ext3")
    res = B.prepare_backstop([str(tmp_path / "empty" / "*")],
                             trash_root=str(tmp_path / "trash"))
    assert res.undoable is False
