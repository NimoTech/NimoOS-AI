import os
import shutil
import sqlite3
import pytest
from skills import shell
from fs import sandbox_view

pytestmark = pytest.mark.asyncio

_HAVE_BWRAP = bool(shutil.which("bwrap") and shutil.which("prlimit"))
skip_no_bwrap = pytest.mark.skipif(not _HAVE_BWRAP, reason="bwrap/prlimit not installed")


def _setup(tmp_path, monkeypatch, visible):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT, "
                 "title TEXT, created_at INT, updated_at INT, "
                 "network_granted INTEGER NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO sessions VALUES ('s1','u',NULL,0,0,0)")
    conn.execute("CREATE TABLE visible_resources (session_id TEXT, path TEXT, kind TEXT)")
    for p, k in visible:
        conn.execute("INSERT INTO visible_resources VALUES (?,?,?)", ("s1", p, k))
    conn.commit()
    monkeypatch.setattr(shell, "WORK_ROOT", tmp_path / "shellroot")
    shell.SESSION_ID_VAR.set("s1")
    shell.DB_VAR.set(conn)
    shell.USER_PATTERNS_VAR.set([])
    return conn


@skip_no_bwrap
async def test_authorized_file_readable(tmp_path, monkeypatch):
    proj = tmp_path / "proj"; proj.mkdir()
    (proj / "hello.txt").write_text("WORLD")
    _setup(tmp_path, monkeypatch, [(str(proj), "folder")])
    out = await shell._run_command_impl(f"cat {proj}/hello.txt", 30, False)
    assert "WORLD" in out


@skip_no_bwrap
async def test_blacklisted_key_not_readable(tmp_path, monkeypatch):
    proj = tmp_path / "proj"; proj.mkdir()
    (proj / "secret.key").write_text("PRIVATE")
    _setup(tmp_path, monkeypatch, [(str(proj), "folder")])
    out = await shell._run_command_impl(f"cat {proj}/secret.key; echo DONE", 30, False)
    assert "PRIVATE" not in out      # masked -> /dev/null (empty)
    assert "DONE" in out


@skip_no_bwrap
async def test_blacklisted_dir_not_readable(tmp_path, monkeypatch):
    proj = tmp_path / "proj"; proj.mkdir()
    (proj / ".ssh").mkdir(); (proj / ".ssh" / "id_rsa").write_text("KEYDATA")
    _setup(tmp_path, monkeypatch, [(str(proj), "folder")])
    out = await shell._run_command_impl(f"cat {proj}/.ssh/id_rsa 2>&1; echo DONE", 30, False)
    assert "KEYDATA" not in out      # .ssh folded to empty tmpfs
    assert "DONE" in out


@skip_no_bwrap
async def test_authorized_dir_is_readonly(tmp_path, monkeypatch):
    proj = tmp_path / "proj"; proj.mkdir()
    (proj / "a.txt").write_text("x")
    _setup(tmp_path, monkeypatch, [(str(proj), "folder")])
    out = await shell._run_command_impl(f"echo hi > {proj}/a.txt 2>&1; echo RC=$?", 30, False)
    assert "RC=0" not in out                          # write rejected (EROFS)
    assert (proj / "a.txt").read_text() == "x"        # host file untouched


@skip_no_bwrap
async def test_unauthorized_path_absent(tmp_path, monkeypatch):
    proj = tmp_path / "proj"; proj.mkdir(); (proj / "ok").write_text("ok")
    secret = tmp_path / "secret"; secret.mkdir(); (secret / "s").write_text("ZSECRET_CANARY")
    _setup(tmp_path, monkeypatch, [(str(proj), "folder")])
    out = await shell._run_command_impl(f"cat {secret}/s 2>&1; echo DONE", 30, False)
    assert "ZSECRET_CANARY" not in out
    assert "DONE" in out


@skip_no_bwrap
async def test_offline_by_default(tmp_path, monkeypatch):
    proj = tmp_path / "proj"; proj.mkdir()
    _setup(tmp_path, monkeypatch, [(str(proj), "folder")])
    out = await shell._run_command_impl(
        "getent hosts example.com >/dev/null 2>&1 && echo NET || echo NONET", 30, False)
    assert "NONET" in out


@skip_no_bwrap
async def test_writable_work_scratch(tmp_path, monkeypatch):
    proj = tmp_path / "proj"; proj.mkdir()
    _setup(tmp_path, monkeypatch, [(str(proj), "folder")])
    out = await shell._run_command_impl("echo data > /work/scratch.txt && cat /work/scratch.txt", 30, False)
    assert "data" in out


@skip_no_bwrap
async def test_many_masks_no_arg_limit(tmp_path, monkeypatch):
    # Disable folding + raise the walk cap so each of the ~1500 secret files
    # becomes an INDIVIDUAL --ro-bind /dev/null mask. This produces a very large
    # bwrap option set, exercising the memfd --args path (which must not hit
    # "Argument list too long"). A plain unmasked file must still be readable.
    monkeypatch.setattr(sandbox_view, "FOLD_THRESHOLD", 10_000)
    monkeypatch.setattr(sandbox_view, "MAX_ENTRIES", 10_000)
    proj = tmp_path / "proj"; proj.mkdir()
    for i in range(1500):
        (proj / f"k{i}.key").write_text("secret")
    (proj / "ok.txt").write_text("FINE")
    _setup(tmp_path, monkeypatch, [(str(proj), "folder")])
    out = await shell._run_command_impl(f"cat {proj}/ok.txt", 30, False)
    assert "FINE" in out
    assert "Argument list too long" not in out
    # a masked key reads empty
    out2 = await shell._run_command_impl(f"cat {proj}/k0.key; echo END", 30, False)
    assert "secret" not in out2 and "END" in out2
