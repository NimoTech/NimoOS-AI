import os
import pwd
import grp
import pytest
from fs import ownership


def _current_username() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


def test_apply_when_user_owns_parent(tmp_path):
    me = _current_username()
    target = tmp_path / "f.txt"
    target.write_text("x")
    # current process owns tmp_path (it created it), so eligible branch fires
    ownership.apply(str(target), me)
    st = os.stat(target)
    assert st.st_uid == os.getuid()


def test_apply_falls_back_to_parent_when_user_unknown(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("x")
    # nonexistent system user: must NOT raise; falls back to parent owner
    ownership.apply(str(target), "definitely-not-a-real-user-xyz123")
    st = os.stat(target)
    par_st = os.stat(tmp_path)
    assert st.st_uid == par_st.st_uid


def test_apply_inherits_parent_mode_for_dirs(tmp_path):
    par = tmp_path / "d"
    par.mkdir(mode=0o750)
    sub = par / "child"
    sub.mkdir()
    ownership.apply(str(sub), _current_username())
    assert (os.stat(sub).st_mode & 0o777) == 0o750


def test_apply_inherits_parent_mode_minus_x_for_files(tmp_path):
    par = tmp_path / "d"
    par.mkdir(mode=0o755)
    f = par / "child.txt"
    f.write_text("x")
    ownership.apply(str(f), _current_username())
    # 0o755 & 0o666 == 0o644
    assert (os.stat(f).st_mode & 0o777) == 0o644
