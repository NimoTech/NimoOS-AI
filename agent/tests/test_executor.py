"""
agent/tests/test_executor.py

Unit + integration tests for agent.netns.executor (socket daemon).

These tests run WITHOUT root — unshare() and config_child_iface() are
monkeypatched to no-ops so the executor binds a real Unix socket in a
temp directory and processes requests normally.

Fixtures:
  executor_server — starts executor.main() in a background thread,
                    monkeypatching the privileged calls.  Yields the
                    socket path.

Test cases:
  test_echo_hi         — "echo hi" → exit 0, "hi" in output
  test_exit_code       — "exit 3"  → exit 3
  test_timeout_kill    — "sleep 10", timeout=1 → killed, timeout note in output
  test_unknown_kind    — kind="other" → error response
  test_output_truncation (unit) — _truncate() on >16 KiB input
  test_env_forwarded   — env {"MY_VAR":"hello"} is visible inside command
  test_cwd_respected   — cwd="/tmp" → pwd returns /tmp

  prlimit availability: prlimit(8) is from util-linux and is expected to be
  present on the target systems.  If not found the tests that actually spawn
  commands are skipped.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import uuid

import pytest

# ---------------------------------------------------------------------------
# prlimit guard
# ---------------------------------------------------------------------------
_prlimit_missing = shutil.which("prlimit") is None


# ---------------------------------------------------------------------------
# Import executor (may fail if implementation not written yet — that is expected
# during the red phase)
# ---------------------------------------------------------------------------
def _import_executor():
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from netns import executor
    return executor


# ---------------------------------------------------------------------------
# Helper: send one request to the executor socket, return parsed response dict
# ---------------------------------------------------------------------------

def _send_request(sock_path: str, req: dict, recv_timeout: float = 15.0) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(sock_path)
        s.settimeout(recv_timeout)
        msg = json.dumps(req) + "\n"
        s.sendall(msg.encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        line = buf.split(b"\n")[0]
        return json.loads(line)


# ---------------------------------------------------------------------------
# Fixture: executor running in background thread with privileged calls stubbed
# ---------------------------------------------------------------------------

@pytest.fixture()
def executor_server(tmp_path, monkeypatch):
    """Start the executor in a background thread with stubs for root calls."""
    executor = _import_executor()

    sock_path = str(tmp_path / "exec.sock")
    pid_path = str(tmp_path / "exec.pid")

    # Patch: unshare → no-op (ctypes call)
    monkeypatch.setattr(executor, "_do_unshare", lambda: None)
    # Patch: config_child_iface → no-op
    import netns.bootstrap as bootstrap
    monkeypatch.setattr(bootstrap, "config_child_iface", lambda: None)
    # Patch env vars for socket path / pid file
    monkeypatch.setenv("NIMOOS_EXEC_SOCK", sock_path)
    monkeypatch.setenv("NIMOOS_EXEC_PID_FILE", pid_path)

    ready = threading.Event()
    exc_holder: list[Exception] = []

    def _run():
        try:
            executor.main(_ready_event=ready)
        except Exception as e:
            exc_holder.append(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    # Wait for the socket to become available (up to 5 s)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if ready.is_set():
            break
        time.sleep(0.05)

    if exc_holder:
        raise exc_holder[0]

    yield sock_path

    # Cleanup: send shutdown by closing — the daemon thread will die naturally


# ---------------------------------------------------------------------------
# Unit test: _truncate helper (no socket, no subprocess)
# ---------------------------------------------------------------------------

def test_truncate_short():
    executor = _import_executor()
    data = b"hello world"
    result = executor._truncate(data, 16 * 1024)
    assert result == "hello world"


def test_truncate_long():
    executor = _import_executor()
    limit = 16 * 1024
    # Generate data that exceeds the limit
    data = b"A" * (limit + 100)
    result = executor._truncate(data, limit)
    assert len(result) <= limit + 60  # allow for the ellipsis marker
    assert "truncated" in result


def test_truncate_exact_boundary():
    executor = _import_executor()
    limit = 16 * 1024
    data = b"X" * limit
    result = executor._truncate(data, limit)
    # Exactly at limit: no truncation
    assert "truncated" not in result
    assert len(result) == limit


# ---------------------------------------------------------------------------
# Integration tests using the socket fixture
# ---------------------------------------------------------------------------

@pytest.mark.skipif(_prlimit_missing, reason="prlimit binary not found")
def test_echo_hi(executor_server):
    req = {
        "id": str(uuid.uuid4()),
        "cmd": "echo hi",
        "timeout_sec": 5,
        "env": {},
        "cwd": "/tmp",
        "kind": "shell",
    }
    resp = _send_request(executor_server, req)
    assert resp["exit"] == 0
    assert "hi" in resp["output"]


@pytest.mark.skipif(_prlimit_missing, reason="prlimit binary not found")
def test_exit_code(executor_server):
    req = {
        "id": str(uuid.uuid4()),
        "cmd": "exit 3",
        "timeout_sec": 5,
        "env": {},
        "cwd": "/tmp",
        "kind": "shell",
    }
    resp = _send_request(executor_server, req)
    assert resp["exit"] == 3


@pytest.mark.skipif(_prlimit_missing, reason="prlimit binary not found")
def test_timeout_kill(executor_server):
    req = {
        "id": str(uuid.uuid4()),
        "cmd": "sleep 60",
        "timeout_sec": 1,
        "env": {},
        "cwd": "/tmp",
        "kind": "shell",
    }
    resp = _send_request(executor_server, req, recv_timeout=10.0)
    # Must be non-zero exit or special marker
    assert resp["exit"] != 0 or "timeout" in resp["output"].lower() or "killed" in resp["output"].lower()


@pytest.mark.skipif(_prlimit_missing, reason="prlimit binary not found")
def test_env_forwarded(executor_server):
    req = {
        "id": str(uuid.uuid4()),
        "cmd": "echo $MY_VAR",
        "timeout_sec": 5,
        "env": {"MY_VAR": "hello_from_test"},
        "cwd": "/tmp",
        "kind": "shell",
    }
    resp = _send_request(executor_server, req)
    assert resp["exit"] == 0
    assert "hello_from_test" in resp["output"]


@pytest.mark.skipif(_prlimit_missing, reason="prlimit binary not found")
def test_cwd_respected(executor_server):
    req = {
        "id": str(uuid.uuid4()),
        "cmd": "pwd",
        "timeout_sec": 5,
        "env": {},
        "cwd": "/tmp",
        "kind": "shell",
    }
    resp = _send_request(executor_server, req)
    assert resp["exit"] == 0
    assert "/tmp" in resp["output"]


def test_unknown_kind(executor_server):
    req = {
        "id": str(uuid.uuid4()),
        "cmd": "echo hi",
        "timeout_sec": 5,
        "env": {},
        "cwd": "/tmp",
        "kind": "other",
    }
    resp = _send_request(executor_server, req)
    assert "error" in resp or resp.get("exit", -1) != 0


@pytest.mark.skipif(_prlimit_missing, reason="prlimit binary not found")
def test_output_truncation_integration(executor_server):
    """Command that produces >16KiB output should be truncated."""
    limit = 16 * 1024
    # Each 'yes' line is 2 bytes ("y\n"); need > 8192 iterations to exceed 16KiB
    req = {
        "id": str(uuid.uuid4()),
        "cmd": f"python3 -c \"print('x'*200)\" ; for i in $(seq 1 200); do python3 -c \"print('A'*100)\"; done",
        "timeout_sec": 10,
        "env": {},
        "cwd": "/tmp",
        "kind": "shell",
    }
    # generate big output
    big_cmd = "python3 -c \"import sys; sys.stdout.write('A' * 20000 + '\\n')\""
    req["cmd"] = big_cmd
    resp = _send_request(executor_server, req)
    assert resp["exit"] == 0
    # Output should be truncated
    assert len(resp["output"]) < 20000
    assert "truncated" in resp["output"]
