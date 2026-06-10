import os
import shutil
import sqlite3
import pytest
from skills import shell

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
async def test_secret_within_authorized_is_readable_pure_mount(tmp_path, monkeypatch):
    # DOCUMENTS pure-mount semantics: files the file tools would hide (*.key,
    # .ssh) ARE readable in the shell. Guardrails = read-only + offline-default.
    # If this fails, masking was reintroduced.
    proj = tmp_path / "proj"; proj.mkdir()
    (proj / "secret.key").write_text("PRIVATE")
    (proj / ".ssh").mkdir(); (proj / ".ssh" / "id_rsa").write_text("KEYDATA")
    _setup(tmp_path, monkeypatch, [(str(proj), "folder")])
    out = await shell._run_command_impl(
        f"cat {proj}/secret.key; cat {proj}/.ssh/id_rsa", 30, False)
    assert "PRIVATE" in out
    assert "KEYDATA" in out


@skip_no_bwrap
async def test_large_dir_deep_file_visible(tmp_path, monkeypatch):
    # The /DATA regression: a big early-sorting dotdir must NOT hide siblings.
    proj = tmp_path / "DATA"; proj.mkdir()
    big = proj / ".system_data"; big.mkdir()
    for i in range(300):
        (big / f"f{i}").write_text("x")
    dl = proj / "Downloads"; dl.mkdir()
    (dl / "report.txt").write_text("DEEP_OK")
    _setup(tmp_path, monkeypatch, [(str(proj), "folder")])
    out = await shell._run_command_impl(f"cat {proj}/Downloads/report.txt", 30, False)
    assert "DEEP_OK" in out


@skip_no_bwrap
async def test_authorized_dir_is_readonly(tmp_path, monkeypatch):
    proj = tmp_path / "proj"; proj.mkdir()
    (proj / "a.txt").write_text("x")
    _setup(tmp_path, monkeypatch, [(str(proj), "folder")])
    out = await shell._run_command_impl(f"echo hi > {proj}/a.txt 2>&1; echo RC=$?", 30, False)
    assert "RC=0" not in out
    assert (proj / "a.txt").read_text() == "x"


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
    out = await shell._run_command_impl(
        "echo data > /work/scratch.txt && cat /work/scratch.txt", 30, False)
    assert "data" in out
