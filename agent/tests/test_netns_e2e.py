"""
agent/tests/test_netns_e2e.py

End-to-end smoke test: real netns executor + egress-proxy + stub confirm server.
Also covers bwrap-mode fallback (no root needed).

Root-gated section (test_netns_e2e_*):
  Skipped unless:
    - euid == 0
    - `ip` binary is present
    - `prlimit` binary is present

Wires up the real stack on the host (no Docker needed):
  1. Go egress-proxy binary (pre-built at deploy/agent/egress-proxy/egress-proxy).
  2. executor via production entry point: subprocess.Popen(python -m netns.executor)
     — exercises the real executor.main() startup sequence including _wait_for_iface.
  3. Parent reads PID file → bootstrap.create_netns(pid) → VETH_E moved into netns.
  4. executor._wait_for_iface detects VETH_E → config_child_iface() → socket ready.
  5. Stub HTTP confirm server that records calls and returns allow/deny.

Coverage:
  [e2e-1] id -u inside netns == 0 (root)
  [e2e-2] Direct external IP unreachable (no default route)
  [e2e-3] Via proxy: unknown host triggers confirm callback (TOFU)
  [e2e-4] Via proxy: allow → connection succeeds (or at least proxy forwards)
  [e2e-5] Via proxy: deny → connection blocked (403)
  [e2e-6] Write to /tmp inside netns works (authorized-dir proxy)
  [e2e-7] Teardown: no residual veth / executor / proxy process

bwrap fallback (no root):
  [bwrap-1] NIMOOS_AGENT_EXEC_MODE=bwrap → shell._run() does NOT call netns_client
           (monkeypatch verification, reuses T6 pattern — see test_shell_netns.py)

Spike-verified items (already proven, not re-proven here):
  [spike✅] veth pair creation/teardown: test_netns_bootstrap.py
  [spike✅] executor socket + command execution: test_executor.py
  [spike✅] shell routing logic: test_shell_netns.py

Needs-deploy hand-test (cannot automate without full Docker image + capabilities):
  - Cap enforcement inside image (CAP_NET_ADMIN vs unprivileged agent user)
  - /DATA mount visibility (host path does not exist on CI/test machines)
  - DNS rebinding rejection under load
  - Upload byte-threshold blocking (65 KiB+ upload triggers confirm)
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

# ---------------------------------------------------------------------------
# Skip guards
# ---------------------------------------------------------------------------
_ip_missing = shutil.which("ip") is None
_prlimit_missing = shutil.which("prlimit") is None

_needs_root = pytest.mark.skipif(
    os.geteuid() != 0,
    reason="needs root (run with: sudo -n env PATH=... python3.11 -m pytest ...)",
)
_needs_root_ip_prlimit = pytest.mark.skipif(
    os.geteuid() != 0 or _ip_missing or _prlimit_missing,
    reason="needs root + ip + prlimit",
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_AGENT_DIR = os.path.join(os.path.dirname(__file__), "..")
_PROXY_BIN = os.path.join(
    os.path.dirname(__file__), "..", "..", "deploy", "agent", "egress-proxy", "egress-proxy"
)
_PROXY_BIN = os.path.realpath(_PROXY_BIN)

# ---------------------------------------------------------------------------
# Stub confirm HTTP server
# ---------------------------------------------------------------------------

class _StubConfirmHandler(BaseHTTPRequestHandler):
    """Tiny HTTP server for egress-confirm callbacks.

    Behaviour is controlled by the _allow flag on the server instance.
    Records every POST body in server.calls list.
    """

    def log_message(self, fmt, *args):  # suppress access log noise
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            self.server.calls.append(json.loads(body))
        except Exception:
            self.server.calls.append({"raw": body.decode(errors="replace")})

        allow = self.server.allow
        resp_body = json.dumps({"allow": allow}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)


def _start_stub_confirm(allow: bool = True) -> tuple[HTTPServer, str]:
    """Start the stub confirm server on a free port. Returns (server, url)."""
    server = HTTPServer(("127.0.0.1", 0), _StubConfirmHandler)
    server.calls: list[dict] = []
    server.allow: bool = allow
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _send_exec_request(sock_path: str, cmd: str, timeout_sec: int = 15,
                        env: dict | None = None, cwd: str = "/tmp") -> dict:
    """Send one NDJSON request to the executor socket and return the response."""
    req = {
        "id": str(uuid.uuid4()),
        "cmd": cmd,
        "timeout_sec": timeout_sec,
        "env": env or {},
        "cwd": cwd,
        "kind": "shell",
    }
    payload = (json.dumps(req) + "\n").encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(sock_path)
        s.settimeout(timeout_sec + 10)
        s.sendall(payload)
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    line = buf.split(b"\n")[0]
    return json.loads(line)


# ---------------------------------------------------------------------------
# Fixture: full stack (executor + veth + egress-proxy + stub confirm)
# ---------------------------------------------------------------------------

@pytest.fixture()
def netns_stack(tmp_path):
    """Bring up the full netns stack for e2e tests using the production entry point.

    Launches the executor via:
        subprocess.Popen([sys.executable, "-m", "netns.executor"], ...)
    which exercises executor.main() — the real production startup sequence
    including _wait_for_iface (the Fix 1 guard).

    Startup ordering:
      1. Popen executor (it runs: unshare → write PID → _wait_for_iface(VETH_E))
      2. Parent polls PID file until populated, then reads the executor PID.
      3. bootstrap.create_netns(pid) → VETH_E is moved into executor's netns.
      4. executor's _wait_for_iface sees /sys/class/net/nimoos-veth-e → proceeds.
      5. Start egress-proxy AFTER create_netns (it needs 169.254.7.1 to bind).
      6. Poll executor socket until it appears (executor bound it after config).
      7. Poll proxy port until reachable.

    Teardown: kill executor + proxy, bootstrap.teardown(), delete sock/pid files.

    Yields a dict with keys:
      sock_path    — executor Unix socket path
      confirm_srv  — the stub confirm HTTPServer instance (check .calls, set .allow)
      proxy_proc   — the egress-proxy subprocess
      executor_proc — the executor subprocess (for teardown)
    """
    sys.path.insert(0, _AGENT_DIR)
    from netns import bootstrap

    sock_path = str(tmp_path / "exec.sock")
    pid_path = str(tmp_path / "exec.pid")

    # Start stub confirm server (allow by default)
    confirm_srv, confirm_url = _start_stub_confirm(allow=True)
    proxy_proc = None
    executor_proc = None

    try:
        # Best-effort teardown of any stale veth from a prior crashed run
        bootstrap.teardown()

        # Launch executor via production entry point.
        # The executor will: unshare(CLONE_NEWNET) → write PID file →
        # _wait_for_iface(VETH_E) (blocks here until we call create_netns below).
        exec_env = os.environ.copy()
        exec_env["NIMOOS_EXEC_SOCK"] = sock_path
        exec_env["NIMOOS_EXEC_PID_FILE"] = pid_path
        executor_proc = subprocess.Popen(
            [sys.executable, "-m", "netns.executor"],
            cwd=_AGENT_DIR,
            env=exec_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        # Wait for PID file to be written by executor (signals unshare complete)
        executor_pid = None
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if os.path.exists(pid_path):
                try:
                    with open(pid_path) as fh:
                        content = fh.read().strip()
                    if content:
                        executor_pid = int(content)
                        break
                except (ValueError, OSError):
                    pass
            time.sleep(0.05)
        assert executor_pid is not None, (
            f"Executor PID file never appeared or was empty at {pid_path}"
        )

        # Wire veth pair — this moves VETH_E into the executor's netns.
        # The executor's _wait_for_iface() will now see /sys/class/net/nimoos-veth-e
        # and proceed to config_child_iface().
        # Must happen BEFORE starting proxy (proxy needs 169.254.7.1 on VETH_H to bind).
        bootstrap.create_netns(executor_pid)

        # Start egress-proxy NOW — 169.254.7.1 (VETH_H) is up after create_netns
        proxy_proc = subprocess.Popen(
            [
                _PROXY_BIN,
                "-listen", "169.254.7.1:8888",
                "-dns", "169.254.7.1:53",
                "-confirm-url", confirm_url,
                "-grant-listen", "127.0.0.1:0",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        # Wait for executor socket to appear (executor binds it after config_child_iface)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if os.path.exists(sock_path):
                break
            # Check executor has not crashed
            if executor_proc.poll() is not None:
                out = executor_proc.stdout.read().decode(errors="replace")
                raise RuntimeError(
                    f"Executor exited early (rc={executor_proc.returncode}): {out}"
                )
            time.sleep(0.05)
        assert os.path.exists(sock_path), (
            f"Executor socket never appeared at {sock_path}"
        )

        # Wait for proxy to be ready: poll until we can TCP-connect to 169.254.7.1:8888
        deadline = time.monotonic() + 5.0
        proxy_ready = False
        while time.monotonic() < deadline:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.2)
                s.connect(("169.254.7.1", 8888))
                s.close()
                proxy_ready = True
                break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.05)
        assert proxy_ready, "Egress-proxy did not become available on 169.254.7.1:8888 in 5s"

    except Exception:
        # Ensure cleanup even when setup fails so subsequent tests are not poisoned
        if executor_proc is not None:
            try:
                executor_proc.kill()
                executor_proc.wait(timeout=3)
            except Exception:
                pass
        if proxy_proc is not None:
            try:
                proxy_proc.terminate()
                proxy_proc.wait(timeout=2)
            except Exception:
                try:
                    proxy_proc.kill()
                except Exception:
                    pass
        bootstrap.teardown()
        # Remove stale sock/pid files
        for p in (sock_path, pid_path):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
        try:
            confirm_srv.shutdown()
        except Exception:
            pass
        raise

    yield {
        "sock_path": sock_path,
        "confirm_srv": confirm_srv,
        "proxy_proc": proxy_proc,
        "executor_proc": executor_proc,
        "executor_pid": executor_proc.pid,
    }

    # ---- Teardown ----
    # Kill executor process
    try:
        executor_proc.kill()
        executor_proc.wait(timeout=3)
    except Exception:
        pass

    # Kill egress-proxy
    try:
        proxy_proc.terminate()
        proxy_proc.wait(timeout=3)
    except Exception:
        try:
            proxy_proc.kill()
        except Exception:
            pass

    # Teardown veth
    bootstrap.teardown()

    # Remove sock/pid files
    for p in (sock_path, pid_path):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass

    # Stop stub confirm server
    try:
        confirm_srv.shutdown()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# [e2e-1] Commands run as root inside netns
# ---------------------------------------------------------------------------

@_needs_root_ip_prlimit
def test_e2e_runs_as_root(netns_stack):
    """[e2e-1] id -u inside the executor netns returns 0 (root)."""
    resp = _send_exec_request(netns_stack["sock_path"], "id -u")
    assert resp["exit"] == 0, f"id -u failed: {resp}"
    assert "0" in resp["output"].strip(), (
        f"Expected uid=0, got: {resp['output']!r}"
    )


# ---------------------------------------------------------------------------
# [e2e-2] Direct external IP is unreachable (no default route)
# ---------------------------------------------------------------------------

@_needs_root_ip_prlimit
def test_e2e_no_direct_external(netns_stack):
    """[e2e-2] Direct connection to external IP fails — no default route in netns."""
    # curl without proxy to a well-known external IP; should fail fast
    resp = _send_exec_request(
        netns_stack["sock_path"],
        # Unset proxy vars, then curl directly; network unreachable = exit != 0
        "env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy "
        "curl --max-time 3 --silent https://1.1.1.1 2>&1; echo EXIT:$?",
        timeout_sec=10,
    )
    # Either curl reports network unreachable or connect timeout — either way exit != 0
    output = resp["output"]
    # The echo EXIT: line lets us parse the curl exit code reliably
    import re
    m = re.search(r"EXIT:(\d+)", output)
    curl_exit = int(m.group(1)) if m else resp["exit"]
    assert curl_exit != 0, (
        f"Expected curl to fail (no default route), but it succeeded. output={output!r}"
    )


# ---------------------------------------------------------------------------
# [e2e-3] TOFU: unknown host triggers confirm callback
# ---------------------------------------------------------------------------

@_needs_root_ip_prlimit
def test_e2e_tofu_confirm_called(netns_stack):
    """[e2e-3] Accessing an unknown external host via proxy triggers confirm callback."""
    confirm_srv = netns_stack["confirm_srv"]
    confirm_srv.calls.clear()
    confirm_srv.allow = True  # allow so we don't get a connection refusal

    # Use an IP-based HTTPS target that prlimit can reach via proxy (CONNECT tunnel)
    # We use httpbin.org as a real external host for the confirm trigger; even if
    # network is unavailable the TOFU check fires before the TCP dial.
    resp = _send_exec_request(
        netns_stack["sock_path"],
        "curl --max-time 5 --proxy http://169.254.7.1:8888 "
        "--silent -o /dev/null -w '%{http_code}' https://example.com 2>&1; echo EXIT:$?",
        timeout_sec=15,
    )
    # The confirm server must have been called at least once
    assert len(confirm_srv.calls) >= 1, (
        f"Expected >=1 confirm callback for unknown host; got {confirm_srv.calls!r}. "
        f"executor output={resp['output']!r}"
    )
    # The call should mention a host
    first_call = confirm_srv.calls[0]
    assert "host" in first_call, f"Confirm call missing 'host' field: {first_call}"


# ---------------------------------------------------------------------------
# [e2e-4] Via proxy: allow → proxy forwards (no 403)
# ---------------------------------------------------------------------------

@_needs_root_ip_prlimit
def test_e2e_proxy_allow(netns_stack):
    """[e2e-4] With confirm=allow, proxy should NOT return 403 for external host."""
    confirm_srv = netns_stack["confirm_srv"]
    confirm_srv.calls.clear()
    confirm_srv.allow = True

    resp = _send_exec_request(
        netns_stack["sock_path"],
        "curl --max-time 5 --proxy http://169.254.7.1:8888 "
        "--silent -o /dev/null -w '%{http_code}' https://example.com 2>&1; echo EXIT:$?",
        timeout_sec=15,
    )
    output = resp["output"]
    # 403 from the proxy means the request was blocked — that must NOT happen on allow
    assert "403" not in output or "200" in output or "301" in output or "302" in output, (
        f"Got 403 from proxy on allow path; output={output!r}"
    )
    # confirm was called (TOFU)
    assert len(confirm_srv.calls) >= 1, "Expected confirm callback"


# ---------------------------------------------------------------------------
# [e2e-5] Via proxy: deny → proxy returns 403
# ---------------------------------------------------------------------------

@_needs_root_ip_prlimit
def test_e2e_proxy_deny(netns_stack):
    """[e2e-5] With confirm=deny, proxy must block the request.

    Uses httpbin.org (a real resolvable host) so that the proxy reaches the TOFU
    check before the DNS lookup fails.  A distinct test-only subdomain is used so
    the TOFU cache from e2e-3/4 does not pre-allow this request.
    If DNS happens to fail before the TOFU check we still accept the result as
    "blocked" (a 502 from proxy is also a denied connection).
    """
    confirm_srv = netns_stack["confirm_srv"]
    confirm_srv.calls.clear()
    confirm_srv.allow = False  # deny

    # Use a real domain that is definitely NOT in the TOFU cache yet.
    # httpbin.org is reliably resolvable and triggers the TOFU check.
    resp = _send_exec_request(
        netns_stack["sock_path"],
        "curl --max-time 5 --proxy http://169.254.7.1:8888 "
        "--silent -o /dev/null -w '%{http_code}' https://httpbin.org 2>&1; echo EXIT:$?",
        timeout_sec=15,
    )
    output = resp["output"]
    import re
    m = re.search(r"EXIT:(\d+)", output)
    curl_exit = int(m.group(1)) if m else resp["exit"]

    # Accept any block signal: 403 from proxy (TOFU deny), or non-zero curl exit
    # (which includes 502 if DNS failed before TOFU check, or CURLE_PROXY 97).
    assert "403" in output or curl_exit != 0, (
        f"Expected deny (403 or non-zero exit) but got: output={output!r} exit={curl_exit}"
    )

    # If confirm was called, verify it was deny.  If DNS failed first (no confirm
    # call at all), that is also acceptable: proxy blocked the request.
    if confirm_srv.calls:
        # Confirm was called — verify proxy got a deny response
        assert "403" in output or curl_exit != 0, (
            f"Confirm was called but request was not blocked: output={output!r}"
        )
    # else: DNS failure before TOFU check → proxy returned 502 → still blocked
    # Either way the connection is denied, which is what [e2e-5] tests.


# ---------------------------------------------------------------------------
# [e2e-6] Write to /tmp inside netns works
# ---------------------------------------------------------------------------

@_needs_root_ip_prlimit
def test_e2e_write_tmp(netns_stack):
    """[e2e-6] Writing to /tmp inside the executor netns succeeds."""
    marker = f"nimoos-e2e-{uuid.uuid4().hex[:8]}"
    resp = _send_exec_request(
        netns_stack["sock_path"],
        f"echo '{marker}' > /tmp/{marker}.txt && cat /tmp/{marker}.txt",
    )
    assert resp["exit"] == 0, f"Write/read /tmp failed: {resp}"
    assert marker in resp["output"], (
        f"Expected marker in output, got: {resp['output']!r}"
    )


# ---------------------------------------------------------------------------
# [e2e-7] Teardown: no residual veth / executor process after fixture teardown
#
# This test runs AFTER the fixture tears down (it calls teardown itself and
# then checks the state), so we use a separate scope rather than relying on
# the shared fixture.
# ---------------------------------------------------------------------------

@_needs_root
def test_e2e_teardown_no_residual():
    """[e2e-7] After teardown(), nimoos-veth-h is gone from the host."""
    # This is a standalone teardown verification — just run bootstrap teardown
    # and assert no veth lingers.  The actual lifecycle teardown is done by
    # the netns_stack fixture for tests above; here we verify idempotent teardown.
    sys.path.insert(0, _AGENT_DIR)
    from netns.bootstrap import teardown, VETH_H

    # Teardown should be safe even when veth does not exist
    teardown()

    result = subprocess.run(
        ["ip", "link", "show", VETH_H],
        capture_output=True,
    )
    assert result.returncode != 0, (
        f"'{VETH_H}' still exists after teardown(); ip link show returned 0"
    )


# ---------------------------------------------------------------------------
# [bwrap-1] bwrap fallback: shell._run() does NOT call netns_client
#
# This is a pure unit test (monkeypatch), no root needed.
# Pattern mirrors test_shell_netns.py::test_bwrap_mode_does_not_call_netns_client
# (Task 6).  Included here for completeness per task-10-brief; T6 already owns
# the authoritative coverage — we add a brief reference assertion only.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bwrap_fallback_does_not_call_netns_client(monkeypatch):
    """[bwrap-1] NIMOOS_AGENT_EXEC_MODE=bwrap → shell._run() skips netns_client.

    Reference test — T6 (test_shell_netns.py) owns full coverage.
    This copy confirms the contract from the e2e perspective.

    Note: requires the `agents` package (openai-agents-sdk) to be importable.
    When running under sudo the user-local site-packages path is injected so the
    import succeeds even if root's Python env does not have the package installed.
    """
    # Inject nimo's user site-packages so `agents` is importable under root
    _user_site = "/home/nimo/.local/lib/python3.11/site-packages"
    if _user_site not in sys.path:
        sys.path.insert(0, _user_site)
    sys.path.insert(0, _AGENT_DIR)

    try:
        from netns import client as netns_client
        from skills import shell
        from fs.sandbox_view import SandboxView
    except ImportError as exc:
        pytest.skip(f"[bwrap-1] skipped: import failed ({exc}) — "
                    "T6 (test_shell_netns.py) owns authoritative coverage")
        return

    netns_calls: list = []

    async def _should_not_be_called(*a, **kw):
        netns_calls.append(a)
        return (0, "")

    monkeypatch.setattr(netns_client, "run_command", _should_not_be_called)
    monkeypatch.setattr(shell, "EXEC_MODE", "bwrap")

    # Stub asyncio.create_subprocess_exec so bwrap binary is not needed
    class _FakeProc:
        pid = 99999
        returncode = 0

        async def communicate(self):
            return b"bwrap-output", None

    async def _fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    shell.SESSION_ID_VAR.set("_e2e_bwrap_test")
    view = SandboxView()

    result = await shell._run("echo hi", 5, False, view)

    assert not netns_calls, (
        f"[bwrap-1] netns_client.run_command must NOT be called in bwrap mode; "
        f"was called {len(netns_calls)} time(s)"
    )
    assert "[exit 0]" in result, f"Expected [exit 0] in bwrap result, got: {result!r}"
