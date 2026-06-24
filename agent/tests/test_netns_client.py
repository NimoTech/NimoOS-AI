"""
agent/tests/test_netns_client.py

Unit + integration tests for agent.netns.client.

These tests reuse the executor_server fixture logic (duplicated to stay
in a standalone file) and call client.run_command() via asyncio.

Test cases:
  test_client_echo        — run_command("echo hi") → (0, output with "hi")
  test_client_exit_code   — run_command("exit 5") → (5, _)
  test_client_timeout     — run_command("sleep 60", timeout=1) → non-zero exit
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
import threading
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# prlimit guard
# ---------------------------------------------------------------------------
_prlimit_missing = shutil.which("prlimit") is None


def _import_executor():
    from netns import executor
    return executor


def _import_client():
    from netns import client
    return client


# ---------------------------------------------------------------------------
# Shared executor fixture (standalone copy so this file has no cross-file dep)
# ---------------------------------------------------------------------------

@pytest.fixture()
def executor_sock(tmp_path, monkeypatch):
    executor = _import_executor()

    sock_path = str(tmp_path / "exec_client.sock")
    pid_path = str(tmp_path / "exec_client.pid")

    monkeypatch.setattr(executor, "_do_unshare", lambda: None)
    import netns.bootstrap as bootstrap
    monkeypatch.setattr(bootstrap, "config_child_iface", lambda: None)
    monkeypatch.setenv("NIMOOS_EXEC_SOCK", sock_path)
    monkeypatch.setenv("NIMOOS_EXEC_PID_FILE", pid_path)

    ready = threading.Event()
    exc_holder: list = []

    def _run():
        try:
            executor.main(_ready_event=ready)
        except Exception as e:
            exc_holder.append(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if ready.is_set():
            break
        time.sleep(0.05)

    if exc_holder:
        raise exc_holder[0]

    yield sock_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.skipif(_prlimit_missing, reason="prlimit binary not found")
def test_client_echo(executor_sock, monkeypatch):
    """client.run_command echoes back output correctly."""
    client = _import_client()
    monkeypatch.setenv("NIMOOS_EXEC_SOCK", executor_sock)

    exit_code, output = asyncio.run(
        client.run_command("echo hi", timeout_sec=5, env={}, cwd="/tmp")
    )
    assert exit_code == 0
    assert "hi" in output


@pytest.mark.skipif(_prlimit_missing, reason="prlimit binary not found")
def test_client_exit_code(executor_sock, monkeypatch):
    """client.run_command returns the correct exit code."""
    client = _import_client()
    monkeypatch.setenv("NIMOOS_EXEC_SOCK", executor_sock)

    exit_code, output = asyncio.run(
        client.run_command("exit 5", timeout_sec=5, env={}, cwd="/tmp")
    )
    assert exit_code == 5


@pytest.mark.skipif(_prlimit_missing, reason="prlimit binary not found")
def test_client_timeout(executor_sock, monkeypatch):
    """client.run_command on a slow command returns within 3x timeout with non-zero exit."""
    client = _import_client()
    monkeypatch.setenv("NIMOOS_EXEC_SOCK", executor_sock)

    async def _run():
        return await asyncio.wait_for(
            client.run_command("sleep 60", timeout_sec=1, env={}, cwd="/tmp"),
            timeout=10.0,
        )

    exit_code, output = asyncio.run(_run())
    # Executor must return exit=124 and the "[killed: timeout ...]" marker.
    assert exit_code == 124
    assert "killed" in output


@pytest.mark.skipif(_prlimit_missing, reason="prlimit binary not found")
def test_client_env(executor_sock, monkeypatch):
    """client.run_command forwards env dict into the command."""
    client = _import_client()
    monkeypatch.setenv("NIMOOS_EXEC_SOCK", executor_sock)

    exit_code, output = asyncio.run(
        client.run_command(
            "echo $TESTVAR",
            timeout_sec=5,
            env={"TESTVAR": "xyzzy"},
            cwd="/tmp",
        )
    )
    assert exit_code == 0
    assert "xyzzy" in output
