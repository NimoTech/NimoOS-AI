"""
agent/tests/test_netns_bootstrap.py

Root-gated integration test for agent.netns.bootstrap.

Skipped when:
  - the process is not running as root (euid != 0), or
  - the `ip` binary is not available.

Test flow (mirrors the validated spike in test_c.sh):
  1. Fork a child process.
  2. Child calls libc.unshare(CLONE_NEWNET) to enter a fresh network namespace.
  3. Child signals the parent via a pipe that it is ready.
  4. Parent calls create_netns(child_pid) to create the veth pair and configure
     the host end.
  5. Parent signals the child that the veth is ready.
  6. Child calls config_child_iface() to configure its end.
  7. Child asserts:
       - "ip route get 8.8.8.8" → output contains "Network is unreachable"
         (no default route, so external addresses are unreachable).
       - "ip route get 169.254.7.1" → output contains "dev nimoos-veth-e"
         (on-link route to the proxy IP is present).
  8. Child writes its assertions result back through a result pipe.
  9. Parent waits for the child, calls teardown(), then asserts that
     "ip link show nimoos-veth-h" exits non-zero (interface gone).

Pipes used for synchronisation:
  r_ready / w_ready  — child → parent "unshare done, assign veth now"
  r_go    / w_go     — parent → child "veth moved, you may configure"
  r_result / w_result — child → parent assertion outcomes (comma-separated)
"""

import ctypes
import os
import shutil
import subprocess
import sys

import pytest

# ---------------------------------------------------------------------------
# Skip guards
# ---------------------------------------------------------------------------
_ip_missing = shutil.which("ip") is None

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0,
    reason="needs root (run with sudo)",
)


@pytest.mark.skipif(_ip_missing, reason="ip binary not found")
def test_netns_bootstrap_full_lifecycle():
    """Full netns bootstrap: veth isolation + teardown."""
    # Import here so import errors surface as failures, not collection errors
    from netns.bootstrap import (
        VETH_E,
        VETH_H,
        config_child_iface,
        create_netns,
        teardown,
    )

    CLONE_NEWNET = 0x40000000
    libc = ctypes.CDLL("libc.so.6", use_errno=True)

    # Pipes for parent <-> child synchronisation
    r_ready, w_ready = os.pipe()   # child → parent: "unshare done"
    r_go, w_go = os.pipe()         # parent → child: "veth ready"
    r_result, w_result = os.pipe() # child → parent: assertion results

    pid = os.fork()

    if pid == 0:
        # ----------------------------------------------------------------
        # CHILD side (will be in new netns)
        # ----------------------------------------------------------------
        # Close file descriptors we don't own
        os.close(r_ready)
        os.close(w_go)
        os.close(r_result)

        try:
            # Enter a new network namespace
            ret = libc.unshare(CLONE_NEWNET)
            if ret != 0:
                errno = ctypes.get_errno()
                os.write(w_result, f"FAIL:unshare errno {errno}".encode())
                os.close(w_result)
                os._exit(1)

            # Signal parent that unshare is done
            os.write(w_ready, b"x")
            os.close(w_ready)

            # Wait for parent to move the veth into our netns
            os.read(r_go, 1)
            os.close(r_go)

            # Configure our side of the netns
            config_child_iface()

            # Assertion 1: external address is unreachable (no default route)
            ext = subprocess.run(
                ["ip", "route", "get", "8.8.8.8"],
                capture_output=True,
                text=True,
            )
            ext_out = (ext.stdout + ext.stderr).strip()

            # Assertion 2: proxy IP is on-link via VETH_E
            onl = subprocess.run(
                ["ip", "route", "get", "169.254.7.1"],
                capture_output=True,
                text=True,
            )
            onl_out = (onl.stdout + onl.stderr).strip()

            # Encode results for the parent
            results = f"{ext_out}|||{onl_out}"
            os.write(w_result, results.encode())
            os.close(w_result)

        except Exception as exc:
            try:
                os.write(w_result, f"FAIL:exception:{exc}".encode())
                os.close(w_result)
            except Exception:
                pass
            os._exit(2)

        os._exit(0)

    # ----------------------------------------------------------------
    # PARENT side (host netns)
    # ----------------------------------------------------------------
    # Close file descriptors we don't own
    os.close(w_ready)
    os.close(r_go)
    os.close(w_result)

    try:
        # Wait for child to finish unshare
        os.read(r_ready, 1)
        os.close(r_ready)

        # Configure veth pair on our (host) side
        create_netns(pid)

        # Tell child veth is ready
        os.write(w_go, b"x")
        os.close(w_go)

        # Read child assertion results
        chunks = []
        while True:
            chunk = os.read(r_result, 4096)
            if not chunk:
                break
            chunks.append(chunk)
        os.close(r_result)

        raw = b"".join(chunks).decode(errors="replace")

        # Reap child
        _, child_status = os.waitpid(pid, 0)
        child_exit = os.WEXITSTATUS(child_status)

        # Now tear down
        teardown()

        # Verify veth is gone
        show_rc = subprocess.run(
            ["ip", "link", "show", VETH_H],
            capture_output=True,
        ).returncode

        # ----------------------------------------------------------------
        # Assertions (parent side, after child has exited)
        # ----------------------------------------------------------------
        assert child_exit == 0, f"Child process exited with status {child_exit}; raw={raw!r}"
        assert "FAIL" not in raw, f"Child reported failure: {raw!r}"

        parts = raw.split("|||")
        assert len(parts) == 2, f"Unexpected result format from child: {raw!r}"
        ext_out, onl_out = parts

        assert "Network is unreachable" in ext_out, (
            f"Expected 'Network is unreachable' for 8.8.8.8, got: {ext_out!r}"
        )
        assert f"dev {VETH_E}" in onl_out, (
            f"Expected 'dev {VETH_E}' in on-link route to 169.254.7.1, got: {onl_out!r}"
        )
        assert show_rc != 0, (
            f"Expected '{VETH_H}' to be gone after teardown(), but 'ip link show' returned 0"
        )

    except Exception:
        # Best-effort cleanup so the interface does not linger
        teardown()
        raise


@pytest.mark.skipif(_ip_missing, reason="ip binary not found")
def test_netns_create_netns_idempotent_with_stale_veth():
    """create_netns must succeed even when a stale veth pair already exists.

    Simulates a force-recreate / SIGKILL scenario where the previous container
    left nimoos-veth-h and nimoos-veth-e behind in the host netns.

    Flow:
      1. Create a stale veth pair (nimoos-veth-h / nimoos-veth-e) in the host
         netns, as if a prior run was killed before teardown().
      2. Fork a child that unshares its network namespace.
      3. Parent calls create_netns(child_pid) — must NOT raise "File exists".
      4. Parent signals child; child calls config_child_iface() — must NOT
         raise "Cannot find device nimoos-veth-e".
      5. Child verifies on-link route to PROXY_IP is present via VETH_E.
      6. Parent tears down and confirms no veth remains.
    """
    from netns.bootstrap import (
        VETH_E,
        VETH_H,
        config_child_iface,
        create_netns,
        teardown,
    )

    CLONE_NEWNET = 0x40000000
    libc = ctypes.CDLL("libc.so.6", use_errno=True)

    # ----------------------------------------------------------------
    # Step 1: inject a stale veth pair (mimics un-cleaned prior run)
    # ----------------------------------------------------------------
    stale = subprocess.run(
        ["ip", "link", "add", VETH_H, "type", "veth", "peer", "name", VETH_E],
        capture_output=True,
    )
    assert stale.returncode == 0, (
        f"Failed to create stale veth pair for test setup: {stale.stderr.decode()!r}"
    )

    # Pipes for synchronisation
    r_ready, w_ready = os.pipe()    # child → parent: "unshare done"
    r_go, w_go = os.pipe()          # parent → child: "veth ready"
    r_result, w_result = os.pipe()  # child → parent: assertion results

    pid = os.fork()

    if pid == 0:
        # ----------------------------------------------------------------
        # CHILD side
        # ----------------------------------------------------------------
        os.close(r_ready)
        os.close(w_go)
        os.close(r_result)

        try:
            ret = libc.unshare(CLONE_NEWNET)
            if ret != 0:
                errno = ctypes.get_errno()
                os.write(w_result, f"FAIL:unshare errno {errno}".encode())
                os.close(w_result)
                os._exit(1)

            os.write(w_ready, b"x")
            os.close(w_ready)

            # Wait for parent to move the veth into our netns
            os.read(r_go, 1)
            os.close(r_go)

            # This must not raise "Cannot find device nimoos-veth-e"
            config_child_iface()

            # Verify on-link route to proxy IP exists
            onl = subprocess.run(
                ["ip", "route", "get", "169.254.7.1"],
                capture_output=True,
                text=True,
            )
            onl_out = (onl.stdout + onl.stderr).strip()
            os.write(w_result, onl_out.encode())
            os.close(w_result)

        except Exception as exc:
            try:
                os.write(w_result, f"FAIL:exception:{exc}".encode())
                os.close(w_result)
            except Exception:
                pass
            os._exit(2)

        os._exit(0)

    # ----------------------------------------------------------------
    # PARENT side
    # ----------------------------------------------------------------
    os.close(w_ready)
    os.close(r_go)
    os.close(w_result)

    try:
        os.read(r_ready, 1)
        os.close(r_ready)

        # Must succeed despite stale veth — this is the core assertion
        create_netns(pid)

        os.write(w_go, b"x")
        os.close(w_go)

        chunks = []
        while True:
            chunk = os.read(r_result, 4096)
            if not chunk:
                break
            chunks.append(chunk)
        os.close(r_result)

        raw = b"".join(chunks).decode(errors="replace")

        _, child_status = os.waitpid(pid, 0)
        child_exit = os.WEXITSTATUS(child_status)

        teardown()

        show_rc = subprocess.run(
            ["ip", "link", "show", VETH_H],
            capture_output=True,
        ).returncode

        # ----------------------------------------------------------------
        # Assertions
        # ----------------------------------------------------------------
        assert "FAIL" not in raw, f"Child reported failure: {raw!r}"
        assert child_exit == 0, f"Child exited with status {child_exit}; raw={raw!r}"
        assert f"dev {VETH_E}" in raw, (
            f"Expected on-link route via '{VETH_E}' in child output, got: {raw!r}"
        )
        assert show_rc != 0, (
            f"Expected '{VETH_H}' to be absent after teardown(), but ip link show returned 0"
        )

    except Exception:
        teardown()
        raise
