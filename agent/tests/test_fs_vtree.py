import os
import pytest
from fs.vtree import VTree, VTreeError


def test_mkdir_then_rename_into_it(tmp_path):
    vt = VTree()
    photos = str(tmp_path / "photos")
    src = tmp_path / "a.jpg"; src.write_text("x")
    vt.mkdir(photos, parents=False)
    vt.rename(str(src), os.path.join(photos, "a.jpg"))  # 不应抛错
    assert vt.exists(os.path.join(photos, "a.jpg"))
    assert not vt.exists(str(src))


def test_recursive_delete_invalidates_subtree(tmp_path):
    a = tmp_path / "A"; (a).mkdir(); (a / "x").write_text("1")
    b = tmp_path / "B.txt"; b.write_text("2")
    vt = VTree()
    vt.delete(str(a), recursive=True)
    with pytest.raises(VTreeError):
        vt.rename(str(b), str(a / "C"))   # 父 /A 已不存在


def test_move_in_then_delete_nonrecursive_rejected(tmp_path):
    a = tmp_path / "A"
    b = tmp_path / "B"; b.write_text("2")
    vt = VTree()
    vt.mkdir(str(a))
    vt.rename(str(b), str(a / "B"))
    with pytest.raises(VTreeError):
        vt.delete(str(a), recursive=False)   # /A 现在非空


def test_circular_move_rejected(tmp_path):
    a = tmp_path / "A"; a.mkdir()
    vt = VTree()
    with pytest.raises(VTreeError):
        vt.rename(str(a), str(a / "B" / "C"))   # 移进自身子树 (EINVAL)


def test_delete_empty_real_dir_allowed(tmp_path):
    a = tmp_path / "A"; a.mkdir()    # 真实磁盘上为空
    vt = VTree()
    vt.delete(str(a), recursive=False)   # 不应抛错
    assert not vt.exists(str(a))


def test_delete_nonempty_real_dir_rejected(tmp_path):
    a = tmp_path / "A"; a.mkdir(); (a / "f").write_text("x")
    vt = VTree()
    with pytest.raises(VTreeError):
        vt.delete(str(a), recursive=False)


def test_rename_src_missing_rejected(tmp_path):
    vt = VTree()
    with pytest.raises(VTreeError):
        vt.rename(str(tmp_path / "nope"), str(tmp_path / "dst"))


def test_rename_dst_exists_rejected(tmp_path):
    a = tmp_path / "a"; a.write_text("1")
    b = tmp_path / "b"; b.write_text("2")
    vt = VTree()
    with pytest.raises(VTreeError):
        vt.rename(str(a), str(b))


def test_mkdir_existing_rejected(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    vt = VTree()
    with pytest.raises(VTreeError):
        vt.mkdir(str(a))


def test_mkdir_missing_parent_without_parents_rejected(tmp_path):
    vt = VTree()
    with pytest.raises(VTreeError):
        vt.mkdir(str(tmp_path / "x" / "y"), parents=False)


def test_rename_unhydrated_dir_preserves_children(tmp_path):
    # Move a real on-disk dir WITHOUT any prior query that would hydrate it.
    a = tmp_path / "A"; a.mkdir(); (a / "child.txt").write_text("x")
    b = tmp_path / "B"
    vt = VTree()
    vt.rename(str(a), str(b))
    assert vt.exists(str(b / "child.txt"))      # moved child still visible
    assert not vt.is_empty_dir(str(b))          # B is non-empty
    with pytest.raises(VTreeError):
        vt.delete(str(b), recursive=False)       # non-empty -> rejected


def test_delete_root_rejected():
    vt = VTree()
    with pytest.raises(VTreeError):
        vt.delete(os.sep, recursive=True)


def test_rename_deep_subtree_preserves_grandchildren(tmp_path):
    deep = tmp_path / "A" / "sub"; deep.mkdir(parents=True)
    (deep / "deep.txt").write_text("y")
    b = tmp_path / "B"
    vt = VTree()
    vt.rename(str(tmp_path / "A"), str(b))
    assert vt.exists(str(b / "sub" / "deep.txt"))   # grandchild visible at new path
    assert not vt.is_empty_dir(str(b / "sub"))
