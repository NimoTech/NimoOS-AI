import os
import signal

import pytest

import agent_md


def _mk(folder, body="# Purpose\nhello\n", *, file_mode=0o644,
        dir_mode=0o755):
    """Create <folder>/agent.md with explicit modes. Returns the md path."""
    os.makedirs(folder, exist_ok=True)
    p = os.path.join(folder, "agent.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(p, file_mode)
    os.chmod(folder, dir_mode)
    return p


# NOTE: every call passes ceiling=str(tmp_path). pytest's tmp_path lives under
# /tmp, which is 1777 — without the ceiling the ancestor walk would reach /tmp
# and report writable_parent for every single fixture.


def test_clean_file_is_loaded(tmp_path):
    folder = str(tmp_path / "proj")
    _mk(folder)
    st = agent_md.probe(folder, ceiling=str(tmp_path))
    assert st.state == agent_md.LOADED
    assert st.reason is None
    assert st.body == "# Purpose\nhello\n"


def test_absent_when_no_file(tmp_path):
    folder = str(tmp_path / "empty")
    os.makedirs(folder)
    st = agent_md.probe(folder, ceiling=str(tmp_path))
    assert st.state == agent_md.ABSENT
    assert st.body is None


def test_world_writable_file_is_skipped(tmp_path):
    folder = str(tmp_path / "proj")
    p = _mk(folder, file_mode=0o666)
    st = agent_md.probe(folder, ceiling=str(tmp_path))
    assert st.state == agent_md.SKIPPED
    assert st.reason == agent_md.WRITABLE_FILE
    assert st.detail == p
    assert st.body is None


def test_group_writable_file_is_skipped(tmp_path):
    folder = str(tmp_path / "proj")
    _mk(folder, file_mode=0o664)
    st = agent_md.probe(folder, ceiling=str(tmp_path))
    assert st.state == agent_md.SKIPPED
    assert st.reason == agent_md.WRITABLE_FILE


def test_writable_own_folder_is_skipped(tmp_path):
    """The folder holding agent.md matters most: if it is writable, anyone
    can replace the file inside it."""
    folder = str(tmp_path / "proj")
    _mk(folder, dir_mode=0o777)
    st = agent_md.probe(folder, ceiling=str(tmp_path))
    assert st.state == agent_md.SKIPPED
    assert st.reason == agent_md.WRITABLE_PARENT
    assert st.detail == os.path.realpath(folder)


def test_writable_grandparent_is_skipped(tmp_path):
    """Pins D3: the walk does not stop at the authorized folder's parent."""
    gp = tmp_path / "shared"
    folder = gp / "mid" / "proj"
    _mk(str(folder))
    os.chmod(str(gp / "mid"), 0o755)
    os.chmod(str(gp), 0o777)
    st = agent_md.probe(str(folder), ceiling=str(tmp_path))
    assert st.state == agent_md.SKIPPED
    assert st.reason == agent_md.WRITABLE_PARENT
    assert st.detail == os.path.realpath(str(gp))


def test_symlink_is_skipped(tmp_path):
    folder = str(tmp_path / "proj")
    os.makedirs(folder)
    target = tmp_path / "elsewhere.md"
    target.write_text("# evil\n", encoding="utf-8")
    os.symlink(str(target), os.path.join(folder, "agent.md"))
    st = agent_md.probe(folder, ceiling=str(tmp_path))
    assert st.state == agent_md.SKIPPED
    assert st.reason == agent_md.SYMLINK


def test_directory_named_agent_md_is_skipped(tmp_path):
    folder = str(tmp_path / "proj")
    os.makedirs(os.path.join(folder, "agent.md"))
    st = agent_md.probe(folder, ceiling=str(tmp_path))
    assert st.state == agent_md.SKIPPED
    assert st.reason == agent_md.NOT_REGULAR


def test_read_body_false_skips_the_read(tmp_path):
    folder = str(tmp_path / "proj")
    _mk(folder)
    st = agent_md.probe(folder, read_body=False, ceiling=str(tmp_path))
    assert st.state == agent_md.LOADED
    assert st.body is None


def test_body_is_capped_at_max_bytes(tmp_path):
    folder = str(tmp_path / "proj")
    _mk(folder, body="x" * 5000)
    st = agent_md.probe(folder, max_bytes=100, ceiling=str(tmp_path))
    assert st.state == agent_md.LOADED
    assert len(st.body) == 100


def test_multibyte_body_is_not_split(tmp_path):
    folder = str(tmp_path / "proj")
    _mk(folder, body="中" * 200)
    st = agent_md.probe(folder, max_bytes=10, ceiling=str(tmp_path))
    assert st.body == "中" * 10
    assert "�" not in st.body


def test_ceiling_defaults_to_filesystem_root(tmp_path):
    """Without a ceiling the walk reaches /tmp (1777) and fails closed."""
    folder = str(tmp_path / "proj")
    _mk(folder)
    st = agent_md.probe(folder)
    assert st.state == agent_md.SKIPPED
    assert st.reason == agent_md.WRITABLE_PARENT


def test_missing_folder_is_absent(tmp_path):
    st = agent_md.probe(str(tmp_path / "nope"), ceiling=str(tmp_path))
    assert st.state == agent_md.ABSENT


def test_fifo_at_agent_md_path_is_skipped_not_hung(tmp_path):
    """A FIFO planted at <folder>/agent.md must not block os.open() forever
    (a one-line `mkfifo` DoS). With O_NONBLOCK the open returns immediately;
    os.fstat then reports a non-regular file and the S_ISREG check fails it
    closed. Guard with signal.alarm so a regression hangs this test loudly
    instead of hanging the whole suite (pytest-timeout is not installed)."""
    folder = str(tmp_path / "proj")
    os.makedirs(folder)
    fifo_path = os.path.join(folder, "agent.md")
    os.mkfifo(fifo_path)

    def _on_alarm(signum, frame):
        raise TimeoutError(
            "probe() hung opening a FIFO — O_NONBLOCK regression"
        )

    old_handler = signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(5)
    try:
        st = agent_md.probe(folder, ceiling=str(tmp_path))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)

    assert st.state == agent_md.SKIPPED
    assert st.reason == agent_md.NOT_REGULAR


def test_symlinked_ancestor_writable_target_is_skipped(tmp_path):
    """A symlinked ancestor must not hide a writable directory behind a
    clean-looking path: _ancestors() realpath's each ancestor before
    checking its mode. Without that realpath call, a refactor could pass
    all other tests while silently reopening the hole this pins."""
    real_writable = tmp_path / "real_writable"
    real_writable.mkdir(mode=0o777)
    os.chmod(str(real_writable), 0o777)  # mkdir mode is masked by umask

    link = tmp_path / "innocuous_link"
    os.symlink(str(real_writable), str(link))

    folder = str(link / "sub" / "proj")
    _mk(folder)

    st = agent_md.probe(folder, ceiling=str(tmp_path))
    assert st.state == agent_md.SKIPPED
    assert st.reason == agent_md.WRITABLE_PARENT
    assert st.detail == os.path.realpath(str(real_writable))
