import os
import tarfile
import pytest
from fs import snapshots as sn


def test_take_file_writes_sidecar(tmp_path):
    src = tmp_path / "f.txt"; src.write_text("hello")
    root = tmp_path / "snap"
    s = sn.SnapshotStore(root=str(root))
    sp = s.take_file("sess", "run", "1", str(src))
    assert os.path.exists(sp)
    assert open(sp, "rb").read() == b"hello"


def test_take_file_size_cap(tmp_path):
    src = tmp_path / "f.bin"
    src.write_bytes(b"\0" * (51 * 1024 * 1024))   # 51 MiB
    s = sn.SnapshotStore(root=str(tmp_path / "snap"), max_bytes=50 * 1024 * 1024)
    with pytest.raises(sn.SnapshotTooLarge):
        s.take_file("sess", "run", "1", str(src))


def test_restore_file_overrides_existing(tmp_path):
    src = tmp_path / "f.txt"; src.write_text("v1")
    s = sn.SnapshotStore(root=str(tmp_path / "snap"))
    sp = s.take_file("sess", "run", "1", str(src))
    src.write_text("v2-modified")
    s.restore_file(sp, str(src))
    assert src.read_text() == "v1"


def test_take_tar_round_trips_directory(tmp_path):
    d = tmp_path / "d"
    (d / "sub").mkdir(parents=True)
    (d / "a.txt").write_text("a")
    (d / "sub" / "b.txt").write_text("b")
    s = sn.SnapshotStore(root=str(tmp_path / "snap"))
    sp = s.take_tar("sess", "run", "1", str(d))
    assert tarfile.is_tarfile(sp)
    # Wipe and restore
    import shutil
    shutil.rmtree(d)
    s.restore_tar(sp, str(d))
    assert (d / "a.txt").read_text() == "a"
    assert (d / "sub" / "b.txt").read_text() == "b"


def test_take_tar_size_cap(tmp_path):
    d = tmp_path / "big"; d.mkdir()
    (d / "blob").write_bytes(b"\0" * (51 * 1024 * 1024))
    s = sn.SnapshotStore(root=str(tmp_path / "snap"),
                          max_bytes=50 * 1024 * 1024)
    with pytest.raises(sn.SnapshotTooLarge):
        s.take_tar("sess", "run", "1", str(d))


def test_prune_run_dir(tmp_path):
    s = sn.SnapshotStore(root=str(tmp_path / "snap"))
    src = tmp_path / "f.txt"; src.write_text("x")
    s.take_file("sess", "run-A", "1", str(src))
    s.prune_run("sess", "run-A")
    assert not os.path.exists(os.path.join(str(tmp_path / "snap"), "sess", "run-A"))


def test_prune_session_dir(tmp_path):
    s = sn.SnapshotStore(root=str(tmp_path / "snap"))
    src = tmp_path / "f.txt"; src.write_text("x")
    s.take_file("sess", "run-A", "1", str(src))
    s.prune_session("sess")
    assert not os.path.exists(os.path.join(str(tmp_path / "snap"), "sess"))
