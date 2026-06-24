"""Tests for shell.py netns execution mode (Task 6).

These tests run without root / without bwrap / without a live executor socket.
They monkeypatch netns_client.run_command and asyncio.create_subprocess_exec
to verify the routing logic only.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

import db as dbmod
from skills import shell
from netns import client as netns_client


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _DummyView:
    """Minimal SandboxView stand-in."""


def _mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT, "
        "title TEXT, created_at INT, updated_at INT, "
        "network_granted INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute("INSERT INTO sessions VALUES ('s1','u',NULL,0,0,0)")
    conn.execute(
        "CREATE TABLE visible_resources (session_id TEXT, path TEXT, kind TEXT)"
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# netns mode: _run() routes through netns_client.run_command
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_netns_mode_calls_client_and_formats_output(monkeypatch):
    """netns mode: _run() calls netns_client.run_command, returns [exit N]\noutput."""
    calls = []

    async def _fake_run_command(cmd, timeout_sec, env, cwd):
        calls.append({"cmd": cmd, "timeout_sec": timeout_sec, "cwd": cwd})
        return (0, "ok")

    monkeypatch.setattr(netns_client, "run_command", _fake_run_command)
    monkeypatch.setattr(shell, "EXEC_MODE", "netns")

    shell.SESSION_ID_VAR.set("s1")
    from fs.sandbox_view import SandboxView
    view = SandboxView()

    result = await shell._run("echo hi", 5, False, view)

    assert result == "[exit 0]\nok"
    assert len(calls) == 1
    assert calls[0]["cmd"] == "echo hi"
    assert calls[0]["timeout_sec"] == 5
    # cwd must be a string (work dir path)
    assert isinstance(calls[0]["cwd"], str)


@pytest.mark.asyncio
async def test_netns_mode_nonzero_exit_still_formats_correctly(monkeypatch):
    """netns mode: non-zero exit code is correctly included in return value."""
    async def _fake_run_command(cmd, timeout_sec, env, cwd):
        return (1, "error output")

    monkeypatch.setattr(netns_client, "run_command", _fake_run_command)
    monkeypatch.setattr(shell, "EXEC_MODE", "netns")

    shell.SESSION_ID_VAR.set("s1")
    from fs.sandbox_view import SandboxView
    view = SandboxView()

    result = await shell._run("bad_cmd", 5, False, view)
    assert result == "[exit 1]\nerror output"


@pytest.mark.asyncio
async def test_netns_mode_timeout_passthrough(monkeypatch):
    """netns mode: executor timeout (exit 124) is passed through as-is."""
    async def _fake_run_command(cmd, timeout_sec, env, cwd):
        return (124, "[killed: timeout 5s]\n")

    monkeypatch.setattr(netns_client, "run_command", _fake_run_command)
    monkeypatch.setattr(shell, "EXEC_MODE", "netns")

    shell.SESSION_ID_VAR.set("s1")
    from fs.sandbox_view import SandboxView
    view = SandboxView()

    result = await shell._run("sleep 999", 5, False, view)
    assert result == "[exit 124]\n[killed: timeout 5s]\n"


# ---------------------------------------------------------------------------
# bwrap mode: _run() does NOT call netns_client; uses asyncio.create_subprocess_exec
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bwrap_mode_does_not_call_netns_client(monkeypatch):
    """bwrap mode: _run() must not call netns_client.run_command."""
    netns_called = []

    async def _should_not_be_called(*a, **kw):
        netns_called.append(True)
        return (0, "")

    monkeypatch.setattr(netns_client, "run_command", _should_not_be_called)
    monkeypatch.setattr(shell, "EXEC_MODE", "bwrap")

    # Intercept asyncio.create_subprocess_exec so bwrap doesn't need to exist.
    class _FakeProc:
        pid = 99999
        returncode = 0
        async def communicate(self):
            return b"bwrap output", None

    async def _fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    shell.SESSION_ID_VAR.set("s1")
    from fs.sandbox_view import SandboxView
    view = SandboxView()

    result = await shell._run("echo hi", 5, False, view)

    assert not netns_called, "netns_client.run_command must NOT be called in bwrap mode"
    assert "[exit 0]" in result


# ---------------------------------------------------------------------------
# _run_command_impl: netns mode skips _maybe_grant_network
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_netns_mode_impl_skips_network_grant(monkeypatch):
    """In netns mode, _run_command_impl must not call _maybe_grant_network."""
    grant_called = []

    async def _fake_grant(session_id, command):
        grant_called.append(True)
        return False

    async def _fake_run(cmd, t, net, view):
        return "[exit 0]\n"

    monkeypatch.setattr(shell, "EXEC_MODE", "netns")
    monkeypatch.setattr(shell, "_maybe_grant_network", _fake_grant)
    monkeypatch.setattr(shell, "_run", _fake_run)

    conn = _mem_db()
    shell.SESSION_ID_VAR.set("s1")
    shell.DB_VAR.set(conn)

    # Even with network=True, grant should not be called in netns mode
    result = await shell._run_command_impl("curl x", 30, True)
    assert not grant_called, "_maybe_grant_network must NOT be called in netns mode"
    assert result == "[exit 0]\n"


@pytest.mark.asyncio
async def test_netns_mode_impl_no_network_hint_on_failure(monkeypatch):
    """In netns mode, failed commands must NOT get the offline network hint."""
    async def _fake_run(cmd, t, net, view):
        return "[exit 6]\ncurl: could not resolve host"

    monkeypatch.setattr(shell, "EXEC_MODE", "netns")
    monkeypatch.setattr(shell, "_run", _fake_run)

    conn = _mem_db()
    shell.SESSION_ID_VAR.set("s1")
    shell.DB_VAR.set(conn)

    result = await shell._run_command_impl("curl x", 30, False)
    assert "network=true" not in result, "netns mode must not append offline hint"


# ---------------------------------------------------------------------------
# bwrap mode: _run_command_impl still calls _maybe_grant_network (regression)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bwrap_mode_impl_still_uses_network_grant(monkeypatch):
    """In bwrap mode, _run_command_impl must still call _maybe_grant_network."""
    grant_called = []

    async def _fake_grant(session_id, command):
        grant_called.append(True)
        return False  # deny

    async def _fake_run(cmd, t, net, view):
        return "[exit 0]\n"

    monkeypatch.setattr(shell, "EXEC_MODE", "bwrap")
    monkeypatch.setattr(shell, "_maybe_grant_network", _fake_grant)
    monkeypatch.setattr(shell, "_run", _fake_run)

    conn = _mem_db()
    shell.SESSION_ID_VAR.set("s1")
    shell.DB_VAR.set(conn)

    # network=True in bwrap mode must trigger grant check
    result = await shell._run_command_impl("curl x", 30, True)
    assert grant_called, "_maybe_grant_network must be called in bwrap mode"
    assert "拒绝" in result


# ---------------------------------------------------------------------------
# m-1: netns mode must NOT call build_view (saves a DB round-trip)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_netns_mode_impl_skips_build_view(monkeypatch):
    """In netns mode, _run_command_impl must not call build_view.

    build_view queries the DB to assemble the sandbox mount list; it is only
    needed in bwrap mode.  In netns mode we short-circuit before the bwrap
    block, so build_view must never be called — verifying m-1.
    """
    build_view_called = []

    def _counting_build_view(session_id, db, user_patterns):
        build_view_called.append((session_id, db, user_patterns))
        from fs.sandbox_view import SandboxView
        return SandboxView()

    async def _fake_run(cmd, t, net, view):
        # In netns mode view should be None (not passed from build_view)
        assert view is None, f"netns _run received a non-None view: {view!r}"
        return "[exit 0]\nok"

    monkeypatch.setattr(shell, "EXEC_MODE", "netns")
    monkeypatch.setattr(shell, "build_view", _counting_build_view)
    monkeypatch.setattr(shell, "_run", _fake_run)

    conn = _mem_db()
    shell.SESSION_ID_VAR.set("s1")
    shell.DB_VAR.set(conn)

    result = await shell._run_command_impl("echo hi", 30, False)
    assert not build_view_called, (
        f"build_view must NOT be called in netns mode; was called {len(build_view_called)} time(s)"
    )
    assert result == "[exit 0]\nok"


@pytest.mark.asyncio
async def test_bwrap_mode_impl_still_calls_build_view(monkeypatch):
    """In bwrap mode, _run_command_impl must still call build_view (regression guard)."""
    build_view_called = []

    def _counting_build_view(session_id, db, user_patterns):
        build_view_called.append(session_id)
        from fs.sandbox_view import SandboxView
        return SandboxView()

    async def _fake_run(cmd, t, net, view):
        return "[exit 0]\n"

    async def _fake_grant(session_id, command):
        return False

    monkeypatch.setattr(shell, "EXEC_MODE", "bwrap")
    monkeypatch.setattr(shell, "build_view", _counting_build_view)
    monkeypatch.setattr(shell, "_run", _fake_run)
    monkeypatch.setattr(shell, "_maybe_grant_network", _fake_grant)

    conn = _mem_db()
    shell.SESSION_ID_VAR.set("s1")
    shell.DB_VAR.set(conn)

    await shell._run_command_impl("echo hi", 30, False)
    assert build_view_called, "build_view must be called in bwrap mode"
