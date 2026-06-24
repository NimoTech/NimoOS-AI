"""
agent/netns/client.py

Async client for the netns executor daemon.

Usage:
    exit_code, output = await run_command("echo hi", timeout_sec=5, env={}, cwd="/tmp")

The client connects to the Unix-domain socket at NIMOOS_EXEC_SOCK
(default /var/run/nimoos/agent-exec.sock), sends a NDJSON request,
and reads one NDJSON response line.

A synchronous wrapper sync_run_command() is provided for callers that
cannot use async (e.g., test fixtures using asyncio.get_event_loop()).
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

DEFAULT_SOCK_PATH = "/var/run/nimoos/agent-exec.sock"


def _get_sock_path() -> str:
    return os.environ.get("NIMOOS_EXEC_SOCK", DEFAULT_SOCK_PATH)


async def run_command(
    cmd: str,
    timeout_sec: int = 30,
    env: dict | None = None,
    cwd: str = "/tmp",
) -> tuple[int, str]:
    """Send *cmd* to the executor daemon and return (exit_code, output).

    Parameters
    ----------
    cmd:
        Shell command string to run inside the sandboxed netns.
    timeout_sec:
        Per-command wall-clock limit (seconds).  Passed to the executor which
        enforces it; the client itself waits for ``timeout_sec + 10`` seconds
        before giving up on the socket receive.
    env:
        Extra environment variables to inject (merged on top of the executor's
        base environment which already contains HTTP_PROXY/HOME etc.).
    cwd:
        Working directory for the command (must exist inside the executor's
        mount namespace).

    Returns
    -------
    (exit_code, output)
        exit_code is the shell exit code; output is combined stdout+stderr
        (possibly truncated to 16 KiB with a "[...truncated N chars...]" marker).
        On internal executor error exit_code is -1.  On timeout-kill exit_code
        is 124 and output begins with "[killed: timeout Ns]".
    """
    sock_path = _get_sock_path()
    req = {
        "id": str(uuid.uuid4()),
        "cmd": cmd,
        "timeout_sec": timeout_sec,
        "env": env or {},
        "cwd": cwd,
        "kind": "shell",
    }
    payload = (json.dumps(req) + "\n").encode()

    # Client-side timeout: executor's timeout + 10 s grace
    client_timeout = timeout_sec + 10

    reader, writer = await asyncio.open_unix_connection(sock_path)
    try:
        writer.write(payload)
        await writer.drain()

        line = await asyncio.wait_for(reader.readline(), timeout=client_timeout)
        resp = json.loads(line)

        if "error" in resp:
            return -1, resp["error"]

        return int(resp.get("exit", -1)), resp.get("output", "")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
