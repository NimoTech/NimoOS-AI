"""
agent/netns/executor.py

Sandboxed executor daemon.

Entry point: main()

Startup sequence:
  1. libc.unshare(CLONE_NEWNET) — enter a fresh network namespace.
  2. Write os.getpid() to the PID file so the parent process can call
     bootstrap.create_netns(pid) to wire up the veth pair.
  2b. Poll /sys/class/net/nimoos-veth-e (up to 10 s) until the parent has
     moved VETH_E into this netns via create_netns().
  3. Call bootstrap.config_child_iface() to configure lo + VETH_E inside the
     new netns.
  4. Listen on a Unix-domain socket for NDJSON command requests.
  5. For each connection: read one JSON request, execute the command, return
     a JSON response.

Request schema (NDJSON, one JSON object per line):
  {
    "id":          str,          — echoed back in response
    "cmd":         str,          — shell command string (kind="shell" only)
    "command":     str,          — executable path (kind="mcp_stdio" only)
    "args":        list[str],    — argv (kind="mcp_stdio" only)
    "timeout_sec": int,          — max wall-clock seconds (capped at MAX_TIMEOUT_SEC)
    "env":         dict[str,str],— extra environment variables (merged into base env)
    "cwd":         str,          — working directory for the command
    "kind":        str           — "shell" or "mcp_stdio"
  }

Response schema:
  {
    "id":     str,   — echoed from request
    "exit":   int,   — process exit code; -1 on internal error; 124 on timeout
    "output": str    — combined stdout+stderr, truncated to MAX_OUTPUT_BYTES
  }
  kind="mcp_stdio" response:
  {
    "id":          str,  — echoed from request
    "socket_path": str,  — unix socket path that bridges process stdin/stdout
    "pid":         int   — process PID (for later stop requests)
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
  NPROC             = 1024

Environment overrides (for testing / deployment):
  NIMOOS_EXEC_SOCK      — Unix socket path (default /var/run/nimoos/agent-exec.sock)
  NIMOOS_EXEC_PID_FILE  — PID file path   (default /var/run/nimoos/agent-exec.pid)
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import select
import signal
import socket
import subprocess
import threading
import time
import uuid

from netns import bootstrap

logger = logging.getLogger(__name__)

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
MCP_SOCK_DIR = os.environ.get("NIMOOS_MCP_SOCK_DIR", "/var/run/nimoos")

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


def _wait_for_iface(name: str, timeout: float = 10.0) -> None:
    """Wait until network interface *name* appears in the current netns.

    Uses ``ip link show <name>`` (netlink) rather than /sys/class/net/<name>
    because /sys is not remounted per-netns after unshare(CLONE_NEWNET) and
    therefore reflects the *host* namespace's interfaces, causing a false
    positive before the parent has moved VETH_E into this netns.  ``ip link``
    queries the kernel via netlink and strictly reflects the calling process's
    current network namespace.

    Raises:
        RuntimeError: if the interface does not appear within *timeout* seconds.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["ip", "link", "show", name],
            capture_output=True,
        )
        if result.returncode == 0:
            return
        time.sleep(0.05)
    logger.error(
        "executor: timed out waiting %gs for interface %r to appear in netns", timeout, name
    )
    raise RuntimeError(
        f"timed out waiting {timeout}s for interface {name!r} to appear in netns"
    )


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
# MCP stdio bridge
# ---------------------------------------------------------------------------

def _build_proxy_env(extra_env: dict) -> dict:
    """Build execution environment with injected proxy vars.

    Mirrors the env-building logic in _execute (shell) so that MCP stdio
    server processes also have their egress routed through the proxy.

    Priority (lowest → highest):
      1. Hard defaults (HOME, PATH, TERM)
      2. Runtime vars from executor os.environ (npm_config_cache, UV_CACHE_DIR,
         NIMOOS_MCP_HOME, passthrough LANG/LC_*/TZ/TMPDIR) — lets the executor's
         own environment seed npm/uv cache dirs even if the client didn't pass them
      3. extra_env from caller (client-computed _stdio_env) — wins over defaults
      4. Proxy vars — ALWAYS last, non-negotiable (Task 5 security invariant)
    """
    base_env = {
        "HOME": "/work",
        "PATH": "/usr/bin:/usr/sbin:/bin:/sbin",
        "TERM": "dumb",
    }
    # Layer 2: runtime vars from the executor's own environment.
    # These seed npm/uv cache paths and locale vars so MCP server sub-processes
    # work correctly even when the client side didn't forward them.
    _RUNTIME_PASSTHROUGH = (
        "npm_config_cache", "UV_CACHE_DIR", "NIMOOS_MCP_HOME",
        "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR",
    )
    for k in _RUNTIME_PASSTHROUGH:
        v = os.environ.get(k)
        if v:
            base_env[k] = v
    # Also pull HOME from NIMOOS_MCP_HOME if available (mirrors client.py _stdio_env)
    mcp_home = os.environ.get("NIMOOS_MCP_HOME")
    if mcp_home:
        base_env["HOME"] = mcp_home

    # Layer 3: caller-supplied env (highest-priority except proxy).
    base_env.update(extra_env)

    # Layer 4: Proxy vars MUST come last — always override anything the caller
    # supplied. This is the Task 5 security invariant: the egress choke-point
    # cannot be bypassed by passing crafted env vars.
    base_env["HTTP_PROXY"] = PROXY_BASE_URL
    base_env["HTTPS_PROXY"] = PROXY_BASE_URL
    base_env["http_proxy"] = PROXY_BASE_URL
    base_env["https_proxy"] = PROXY_BASE_URL
    base_env["NO_PROXY"] = ""
    base_env["no_proxy"] = ""
    return base_env


def _bridge_pipe_to_socket(src, dst, stop_event: threading.Event) -> None:
    """Copy bytes from file-like *src* (read) to socket *dst* (send) until EOF or stop."""
    try:
        while not stop_event.is_set():
            ready, _, _ = select.select([src], [], [src], 0.5)
            if not ready:
                continue
            chunk = os.read(src.fileno(), 4096)
            if not chunk:
                break
            dst.sendall(chunk)
    except (OSError, BrokenPipeError):
        pass
    finally:
        stop_event.set()


def _bridge_socket_to_pipe(src: socket.socket, dst, stop_event: threading.Event) -> None:
    """Copy bytes from socket *src* (recv) to file-like *dst* (write) until EOF or stop."""
    try:
        while not stop_event.is_set():
            ready, _, _ = select.select([src], [], [src], 0.5)
            if not ready:
                continue
            chunk = src.recv(4096)
            if not chunk:
                break
            dst.write(chunk)
            dst.flush()
    except (OSError, BrokenPipeError):
        pass
    finally:
        stop_event.set()


def _serve_mcp_socket(
    server_sock: socket.socket,
    proc: subprocess.Popen,
    stop_event: threading.Event,
    sock_path: str,
) -> None:
    """Accept one connection on *server_sock*, bridge it to *proc* stdin/stdout.

    This runs in a daemon thread.  When the connection closes or the process
    exits, stop_event is set and the thread returns.  The socket file at
    *sock_path* is always unlinked on exit (I-1 fix).  The MCP server process
    *proc* is always reaped on exit (I-2 fix).
    """
    try:
        server_sock.settimeout(30.0)
        conn, _ = server_sock.accept()
        conn.setblocking(True)
        stop_event.clear()

        # Two threads: one for each direction
        t_out = threading.Thread(
            target=_bridge_pipe_to_socket,
            args=(proc.stdout, conn, stop_event),
            daemon=True,
        )
        t_in = threading.Thread(
            target=_bridge_socket_to_pipe,
            args=(conn, proc.stdin, stop_event),
            daemon=True,
        )
        t_out.start()
        t_in.start()
        # Wait until either direction closes
        stop_event.wait()
        t_out.join(timeout=1.0)
        t_in.join(timeout=1.0)
    except OSError:
        pass
    finally:
        # I-1: close proc pipes first so the process sees EOF, then reap it.
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.stdout.close()
        except Exception:
            pass
        # I-2: reap the MCP server process — terminate → wait → kill if needed.
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait()
            except Exception:
                pass
        except Exception:
            pass
        # I-1: close and unlink the per-server socket file.
        try:
            server_sock.close()
        except Exception:
            pass
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
        except Exception:
            pass


def _execute_mcp_stdio(req: dict) -> dict:
    """Spawn an MCP stdio server in the current netns and bridge it to a Unix socket.

    The subprocess inherits the executor's network namespace, ensuring all
    outbound connections are subject to the same egress controls as shell commands.

    Returns:
        {"id": ..., "socket_path": ..., "pid": ...}  on success
        {"id": ..., "error": ...}                     on failure
    """
    req_id = req.get("id", "")
    command = req.get("command", "")
    args = req.get("args") or []
    extra_env = req.get("env") or {}

    if not command:
        return {"id": req_id, "error": "mcp_stdio: 'command' is required"}

    proc_env = _build_proxy_env(extra_env)

    # Per-server socket path: unique per request
    sock_name = f"agent-mcp-{uuid.uuid4().hex[:12]}.sock"
    sock_dir = MCP_SOCK_DIR
    os.makedirs(sock_dir, exist_ok=True)
    sock_path = os.path.join(sock_dir, sock_name)

    # Remove any stale socket
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass

    # Bind the Unix socket before spawning the process so the client can connect
    # immediately after receiving the socket_path.
    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(sock_path)
    server_sock.listen(1)

    try:
        proc = subprocess.Popen(
            [command] + list(args),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=proc_env,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        server_sock.close()
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
        return {"id": req_id, "error": f"mcp_stdio spawn error: {exc}"}

    stop_event = threading.Event()

    # Bridge thread: accept one connection and relay stdin/stdout ↔ socket.
    # sock_path is passed so _serve_mcp_socket can unlink it on exit (I-1).
    bridge_thread = threading.Thread(
        target=_serve_mcp_socket,
        args=(server_sock, proc, stop_event, sock_path),
        daemon=True,
    )
    bridge_thread.start()

    return {"id": req_id, "socket_path": sock_path, "pid": proc.pid}


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------

def _execute(req: dict) -> dict:
    """Dispatch *req* to the appropriate handler and return a response dict."""
    req_id = req.get("id", "")
    kind = req.get("kind", "shell")

    if kind == "mcp_stdio":
        return _execute_mcp_stdio(req)

    if kind != "shell":
        return {"id": req_id, "error": f"unsupported kind: {kind!r}"}

    cmd = req.get("cmd", "")
    raw_timeout = req.get("timeout_sec", DEFAULT_TIMEOUT_SEC)
    timeout_sec = max(1, min(int(raw_timeout), MAX_TIMEOUT_SEC))
    extra_env = req.get("env") or {}
    cwd = req.get("cwd") or "/work"

    # Build execution environment.
    # Start with defaults, merge caller-supplied extras, then force-override
    # proxy variables so a malicious caller cannot bypass the egress choke-point
    # by passing env={"HTTP_PROXY": "http://evil"}.
    base_env = {
        "HOME": "/work",
        "PATH": "/usr/bin:/usr/sbin:/bin:/sbin",
        "TERM": "dumb",
    }
    base_env.update(extra_env)
    # Proxy vars MUST come last — always override anything the caller supplied.
    base_env["HTTP_PROXY"] = PROXY_BASE_URL
    base_env["HTTPS_PROXY"] = PROXY_BASE_URL
    base_env["http_proxy"] = PROXY_BASE_URL
    base_env["https_proxy"] = PROXY_BASE_URL
    base_env["NO_PROXY"] = ""
    base_env["no_proxy"] = ""

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

    # 2. Write PID file so the parent process can call bootstrap.create_netns(pid)
    #    to move VETH_E into this network namespace.
    pid_file = os.environ.get("NIMOOS_EXEC_PID_FILE", DEFAULT_PID_FILE)
    pid_dir = os.path.dirname(pid_file)
    if pid_dir:
        os.makedirs(pid_dir, exist_ok=True)
    with open(pid_file, "w") as fh:
        fh.write(str(os.getpid()) + "\n")

    # 2b. Wait for the parent to move VETH_E into this netns.
    #     The parent reads the PID file and calls bootstrap.create_netns(pid)
    #     which runs `ip link set VETH_E netns <pid>`.  Only after that does
    #     /sys/class/net/nimoos-veth-e appear in our namespace.
    _wait_for_iface(bootstrap.VETH_E)

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
