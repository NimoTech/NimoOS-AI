"""
agent/netns/bootstrap.py

Network namespace bootstrap for sandboxed executor processes.

Parent-side (host netns):
    create_netns(executor_pid) — create veth pair, move one end into the
    executor's netns, configure the host-side address.

Child-side (inside new netns):
    config_child_iface() — bring up lo and the veth peer, assign address.
    NO default route is added — this is the core security constraint:
    adding a default route would let the host forward traffic and leak
    egress from the namespace.

Teardown:
    teardown() — explicitly delete the host-side veth.  Do NOT rely on
    netns destruction to remove the veth; that path is asynchronous and
    can race with the next executor launch re-using the same name.
"""

import subprocess

# ---------------------------------------------------------------------------
# Constants — must match egress-proxy expectations
# ---------------------------------------------------------------------------
VETH_H = "nimoos-veth-h"   # host-side veth (stays in host netns)
VETH_E = "nimoos-veth-e"   # executor-side veth (moved into child netns)
PROXY_IP = "169.254.7.1"   # address on VETH_H (host / proxy side)
NS_IP = "169.254.7.2"      # address on VETH_E (executor / child side)
PREFIX = 30                # /30 — just two addresses, no broadcast waste


# ---------------------------------------------------------------------------
# Parent-side: called from the host process after the child has unshared
# ---------------------------------------------------------------------------

def create_netns(executor_pid: int) -> None:
    """Create a veth pair and configure the host end.

    Steps (all run as the calling process, which must have CAP_NET_ADMIN):
    1. Create a veth pair: VETH_H <-> VETH_E (both initially in host netns).
    2. Move VETH_E into the executor's network namespace (identified by PID).
    3. Assign PROXY_IP/PREFIX to VETH_H.
    4. Bring VETH_H up.

    The child end (VETH_E) is configured separately by config_child_iface().
    """
    subprocess.run(
        ["ip", "link", "add", VETH_H, "type", "veth", "peer", "name", VETH_E],
        check=True,
    )
    subprocess.run(
        ["ip", "link", "set", VETH_E, "netns", str(executor_pid)],
        check=True,
    )
    subprocess.run(
        ["ip", "addr", "add", f"{PROXY_IP}/{PREFIX}", "dev", VETH_H],
        check=True,
    )
    subprocess.run(
        ["ip", "link", "set", VETH_H, "up"],
        check=True,
    )


# ---------------------------------------------------------------------------
# Child-side: called from inside the unshared network namespace
# ---------------------------------------------------------------------------

def config_child_iface() -> None:
    """Configure networking inside the executor's network namespace.

    Brings up the loopback interface and the executor-side veth (VETH_E),
    then assigns NS_IP/PREFIX to VETH_E.

    IMPORTANT: No default route is added.  The absence of a default route
    means that packets to arbitrary Internet addresses (e.g. 8.8.8.8) will
    result in "Network is unreachable" rather than being forwarded through
    the host.  Adding a default route here would defeat the egress isolation
    this namespace exists to enforce.
    """
    subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
    subprocess.run(["ip", "link", "set", VETH_E, "up"], check=True)
    subprocess.run(
        ["ip", "addr", "add", f"{NS_IP}/{PREFIX}", "dev", VETH_E],
        check=True,
    )
    # --- no default route ---


# ---------------------------------------------------------------------------
# Teardown: called from the host process after the child has exited
# ---------------------------------------------------------------------------

def teardown() -> None:
    """Explicitly delete the host-side veth interface.

    Deleting VETH_H also removes VETH_E (the kernel removes both ends of a
    veth pair when either is deleted).  We do this explicitly rather than
    relying on the netns being garbage-collected because that collection is
    asynchronous and can race with the next executor reusing the same name.

    Errors are silently ignored so that teardown is always safe to call even
    if setup only partially succeeded or the interface was already removed.
    """
    subprocess.run(
        ["ip", "link", "del", VETH_H],
        capture_output=True,  # suppress error messages on no-op
    )
