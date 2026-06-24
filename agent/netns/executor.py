"""
agent/netns/executor.py

Sandboxed executor daemon.

Entry point: main()

Startup sequence:
  1. libc.unshare(CLONE_NEWNET) — enter a fresh network namespace.
  2. Write os.getpid() to the PID file so the parent process can call
     bootstrap.create_netns(pid) to wire up the veth pair.
  3. Call bootstrap.config_child_iface() to configure lo + VETH_E inside the
     new netns.
  4. Listen on a Unix-domain socket for NDJSON command requests.
  5. For each connection: read one JSON request, execute the command, return
     a JSON response.

Request schema (NDJSON, one JSON object per line):
  {
    "id":          str,          — echoed back in response
    "cmd":         str,          — shell command string
    "timeout_sec": int,          — max wall-clock seconds (capped at MAX_TIMEOUT_SEC)
    "env":         dict[str,str],— extra environment variables (merged into base env)
    "cwd":         str,          — working directory for the command
    "kind":        str           — P0: only "shell" is supported
  }

Response schema:
  {
    "id":     str,   — echoed from request
    "exit":   int,   — process exit code; -1 on internal error; 124 on timeout
    "output": str    — combined stdout+stderr, truncated to MAX_OUTPUT_BYTES
  }
  On unknown kind:
  {
    "id":    str,
    "error": str
  }

Constants:
  MEM_BYTES         = 512 MiB address-space limit passed to prlimit
  MAX_TIMEOUT_SEC   = 300 s
  DEFAULT_TIMEOUT_SEC = 30 s
  MAX_OUTPUT_BYTES  = 16 KiB
  NPROC             = 64

Environment overrides (for testing / deployment):
  NIMOOS_EXEC_SOCK      — Unix socket path (default /var/run/nimoos/agent-exec.sock)
  NIMOOS_EXEC_PID_FILE  — PID file path   (default /var/run/nimoos/agent-exec.pid)
"""
from __future__ import annotations

import ctypes
import json
import os
import signal
import socket
import subprocess
import threading

from netns import bootstrap

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CLONE_NEWNET = 0x40000000

MEM_BYTES = 512 * 1024 * 1024
MAX_TIMEOUT_SEC = 300
DEFAULT_TIMEOUT_SEC = 30
MAX_OUTPUT_BYTES = 16 * 1024
NPROC = 1024  # per-user process limit inside the sandbox

DEFAULT_SOCK_PATH = "/var/run/nimoos/agent-exec.sock"
DEFAULT_PID_FILE = "/var/run/nimoos/agent-exec.pid"

PROXY_BASE_URL = f"http://{bootstrap.PROXY_IP}:8888"

PRLIMIT_BIN = os.environ.get("PRLIMIT_PATH", "/usr/bin/prlimit")


# ---------------------------------------------------------------------------
# Privileged operations (extracted so tests can monkeypatch them)
# ---------------------------------------------------------------------------

def _do_unshare() -> None:
    """Call libc.unshare(CLONE_NEWNET).  Raises OSError on failure."""
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    ret = libc.unshare(CLONE_NEWNET)
    if ret != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))


# ---------------------------------------------------------------------------
# Output truncation (mirrors skills/shell.py::_truncate)
# ---------------------------------------------------------------------------

def _truncate(data: bytes, limit: int) -> str:
    """Decode *data* and truncate to *limit* characters if needed.

    When truncation occurs a marker is inserted in the middle:
        <head> + "\\n[...truncated N chars...]\\n" + <tail>
    """
    text = data.decode("utf-8", errors="replace")
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    dropped = len(text) - limit
    return text[:head] + f"\n[...truncated {dropped} chars...]\n" + text[-tail:]


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------

def _execute(req: dict) -> dict:
    """Run a shell command from *req* and return a response dict."""
    req_id = req.get("id", "")
    kind = req.get("kind", "shell")

    if kind != "shell":
        return {"id": req_id, "error": f"unsupported kind: {kind!r}"}

    cmd = req.get("cmd", "")
    raw_timeout = req.get("timeout_sec", DEFAULT_TIMEOUT_SEC)
    timeout_sec = max(1, min(int(raw_timeout), MAX_TIMEOUT_SEC))
    extra_env = req.get("env") or {}
    cwd = req.get("cwd") or "/work"

    # Build execution environment
    base_env = {
        "HTTP_PROXY": PROXY_BASE_URL,
        "HTTPS_PROXY": PROXY_BASE_URL,
        "http_proxy": PROXY_BASE_URL,
        "https_proxy": PROXY_BASE_URL,
        "NO_PROXY": "",
        "no_proxy": "",
        "HOME": "/work",
        "PATH": "/usr/bin:/usr/sbin:/bin:/sbin",
        "TERM": "dumb",
    }
    base_env.update(extra_env)

    # Build prlimit-wrapped command
    argv = [
        PRLIMIT_BIN,
        f"--as={MEM_BYTES}",
        f"--cpu={MAX_TIMEOUT_SEC}",
        f"--nofile={NOFILE_LIMIT}",
        f"--nproc={NPROC}",
        "--",
        "/bin/bash", "-lc", cmd,
    ]

    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=base_env,
            cwd=cwd if os.path.isdir(cwd) else "/tmp",
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        return {"id": req_id, "exit": -1, "output": f"executor error: {exc}"}

    try:
        stdout, _ = proc.communicate(timeout=timeout_sec)
        output = _truncate(stdout or b"", MAX_OUTPUT_BYTES)
        return {"id": req_id, "exit": proc.returncode, "output": output}
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            stdout, _ = proc.communicate(timeout=5)
        except Exception:
            stdout = b""
        output = _truncate(stdout or b"", MAX_OUTPUT_BYTES)
        output = f"[killed: timeout {timeout_sec}s]\n{output}"
        return {"id": req_id, "exit": 124, "output": output}


# nofile constant (matches shell.py)
NOFILE_LIMIT = 1024


# ---------------------------------------------------------------------------
# Connection handler
# ---------------------------------------------------------------------------

def _handle_connection(conn: socket.socket) -> None:
    """Read one NDJSON request from *conn*, execute it, write one response."""
    try:
        buf = b""
        conn.settimeout(60.0)
        while b"\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk

        line = buf.split(b"\n")[0]
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            resp = {"id": "", "exit": -1, "output": f"bad request JSON: {exc}"}
            conn.sendall((json.dumps(resp) + "\n").encode())
            return

        resp = _execute(req)
        conn.sendall((json.dumps(resp) + "\n").encode())
    except Exception:
        pass  # silently drop on any socket error
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main(*, _ready_event=None) -> None:
    """Executor daemon entry point.

    _ready_event (threading.Event, optional): set once the socket is bound and
        listening — used by tests to know the server is ready to accept.
    """
    # 1. Enter a fresh network namespace
    _do_unshare()

    # 2. Write PID file
    pid_file = os.environ.get("NIMOOS_EXEC_PID_FILE", DEFAULT_PID_FILE)
    pid_dir = os.path.dirname(pid_file)
    if pid_dir:
        os.makedirs(pid_dir, exist_ok=True)
    with open(pid_file, "w") as fh:
        fh.write(str(os.getpid()) + "\n")

    # 3. Configure network inside new netns
    bootstrap.config_child_iface()

    # 4. Bind Unix socket
    sock_path = os.environ.get("NIMOOS_EXEC_SOCK", DEFAULT_SOCK_PATH)
    sock_dir = os.path.dirname(sock_path)
    if sock_dir:
        os.makedirs(sock_dir, exist_ok=True)

    # Remove stale socket
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(sock_path)
    server.listen(64)

    # Signal readiness to test harness
    if _ready_event is not None:
        _ready_event.set()

    # 5. Accept loop — each connection handled in a new thread
    while True:
        try:
            conn, _ = server.accept()
        except OSError:
            break
        t = threading.Thread(target=_handle_connection, args=(conn,), daemon=True)
        t.start()


if __name__ == "__main__":
    main()
