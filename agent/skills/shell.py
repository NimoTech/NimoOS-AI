"""Sandboxed shell tool surface for the agent.

Each ``run_command`` call spawns a fresh ``bwrap`` (bubblewrap) subprocess.
The container has read-only system dirs, a session-persistent ``/work``
directory, ``/tmp`` as tmpfs, and the network namespace shared with the host
so curl/git/apt continue to work. Resource caps come from ``prlimit`` and an
asyncio timeout guards wall time.
"""
from __future__ import annotations

import asyncio
import os
import signal
from contextvars import ContextVar
from pathlib import Path

from agents import function_tool


SESSION_ID_VAR: ContextVar[str] = ContextVar("shell_session_id", default="_default")

BWRAP_BIN = os.environ.get("BWRAP_PATH", "/usr/bin/bwrap")
PRLIMIT_BIN = os.environ.get("PRLIMIT_PATH", "/usr/bin/prlimit")

DEFAULT_TIMEOUT_SEC = 30
MAX_TIMEOUT_SEC = 300
MEM_BYTES = 512 * 1024 * 1024
NOFILE = 1024
MAX_OUTPUT_BYTES = 16 * 1024

WORK_ROOT = Path(os.environ.get(
    "NIMOOS_AGENT_SHELL_ROOT",
    str(Path.home() / ".nimoos" / "agent"),
))


def _work_dir(session_id: str) -> Path:
    p = WORK_ROOT / session_id / "work"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _build_argv(work: Path, command: str) -> list[str]:
    return [
        PRLIMIT_BIN,
        f"--as={MEM_BYTES}",
        f"--cpu={MAX_TIMEOUT_SEC}",
        f"--nofile={NOFILE}",
        BWRAP_BIN,
        "--ro-bind", "/usr", "/usr",
        "--ro-bind-try", "/etc", "/etc",
        "--ro-bind-try", "/opt", "/opt",
        "--ro-bind-try", "/bin", "/bin",
        "--ro-bind-try", "/sbin", "/sbin",
        "--ro-bind-try", "/lib", "/lib",
        "--ro-bind-try", "/lib64", "/lib64",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--bind", str(work), "/work",
        "--chdir", "/work",
        "--unshare-all",
        "--share-net",
        "--die-with-parent",
        "--new-session",
        "--setenv", "HOME", "/work",
        "--setenv", "PATH", "/usr/bin:/usr/sbin:/bin:/sbin",
        "--setenv", "TERM", "dumb",
        "--",
        "/bin/bash", "-lc", command,
    ]


def _truncate(data: bytes, limit: int) -> str:
    text = data.decode("utf-8", errors="replace")
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    dropped = len(text) - limit
    return text[:head] + f"\n[...truncated {dropped} chars...]\n" + text[-tail:]


async def _run(command: str, timeout_sec: int) -> str:
    timeout_sec = max(1, min(int(timeout_sec), MAX_TIMEOUT_SEC))
    session_id = SESSION_ID_VAR.get()
    work = _work_dir(session_id)
    argv = _build_argv(work, command)

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError as e:
        return f"sandbox unavailable: {e}"

    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_sec,
        )
        body = _truncate(stdout or b"", MAX_OUTPUT_BYTES)
        return f"[exit {proc.returncode}]\n{body}"
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            stdout, _ = await proc.communicate()
        except Exception:
            stdout = b""
        body = _truncate(stdout or b"", MAX_OUTPUT_BYTES)
        return f"[killed: timeout {timeout_sec}s]\n{body}"


@function_tool
async def run_command(command: str, timeout_sec: int = DEFAULT_TIMEOUT_SEC) -> str:
    """Run a bash command inside an isolated sandbox (bubblewrap).

    The sandbox provides:
      - read-only /usr,/etc,/lib (no host $HOME, no other services' data)
      - a writable /work directory that persists across calls in the same
        chat session (cwd starts at /work; HOME=/work)
      - /tmp as tmpfs
      - network access (curl/git/apt work normally)

    The result is combined stdout+stderr, truncated to ~16 KiB. The first
    line is `[exit N]` or `[killed: timeout Ns]`. Default timeout 30s,
    maximum 300s; memory cap 512 MiB.
    """
    return await _run(command, timeout_sec)


ALL_TOOLS = [run_command]
