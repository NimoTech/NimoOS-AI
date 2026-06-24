"""Sandboxed shell tool surface for the agent.

Execution mode is controlled by the ``NIMOOS_AGENT_EXEC_MODE`` environment
variable (default ``netns``):

* **netns** (default): Commands are forwarded to the netns executor daemon via
  ``netns.client.run_command``.  The daemon runs commands inside an isolated
  network namespace with a transparent egress proxy; network is available but
  outbound traffic to unknown destinations requires confirmation (DLP managed
  by the proxy layer).  No bwrap is involved.

* **bwrap**: Legacy bubblewrap sandbox.  Each ``run_command`` spawns a fresh
  ``bwrap`` subprocess.  The container has read-only system dirs, a
  session-persistent ``/work``, ``/tmp`` as tmpfs, and — when the user has
  authorized resources — those folders/files mounted READ-ONLY at their real
  paths (with blacklisted subpaths masked).  Network is OFF by default
  (``--unshare-net``); ``network=True`` asks the user to confirm, and once
  granted stays on for the session.  bwrap args are passed via ``--args <fd>``
  (an in-memory memfd) to bypass ARG_MAX and avoid pipe deadlocks.
"""
from __future__ import annotations

import asyncio
import os
import signal
from contextvars import ContextVar
from pathlib import Path

from agents import function_tool

import db as dbmod
from fs.sandbox_view import SandboxView, build_view, to_bwrap_args
from netns import client as netns_client


SESSION_ID_VAR: ContextVar[str] = ContextVar("shell_session_id", default="_default")
SANDBOX_SKILLS_VAR: ContextVar[str] = ContextVar("sandbox_skills", default="")
SANDBOX_SHELL_ROOT_VAR: ContextVar[str] = ContextVar("sandbox_shell_root", default="")
# Set by agent.py::run() before every agent loop (mirror the filesystem skill).
DB_VAR: ContextVar = ContextVar("shell_db", default=None)
USER_PATTERNS_VAR: ContextVar[list] = ContextVar("shell_user_patterns", default=[])
CONFIRM_MGR_VAR: ContextVar = ContextVar("shell_confirm_mgr", default=None)
EVENT_QUEUE_VAR: ContextVar = ContextVar("shell_event_queue", default=None)

EXEC_MODE = os.environ.get("NIMOOS_AGENT_EXEC_MODE", "netns")

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

_NETWORK_HINT = ("\n(System Hint: 命令可能因沙箱默认断网而失败。"
                 "若确需联网,请以 network=true 重试,会请用户确认。)")


def _work_dir(session_id: str) -> Path:
    root = SANDBOX_SHELL_ROOT_VAR.get() or str(WORK_ROOT)
    p = Path(root) / session_id / "work"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _build_bwrap_opts(work: Path, view: SandboxView, network: bool) -> list[str]:
    """bwrap OPTIONS only (these go into the --args fd). The command tail
    (`-- /bin/bash -lc CMD`) is passed on the real argv in _run, because
    bwrap 0.8.0's --args does not accept the command from the fd."""
    args = [
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
    ]
    args += to_bwrap_args(view)
    args += ["--bind", str(work), "/work"]
    skills_view = SANDBOX_SKILLS_VAR.get()
    if skills_view:
        args += ["--ro-bind", skills_view, "/skill"]
    args += [
        "--chdir", "/work",
        "--unshare-all",
        "--share-net" if network else "--unshare-net",
        "--die-with-parent",
        "--new-session",
        "--setenv", "HOME", "/work",
        "--setenv", "PATH", "/usr/bin:/usr/sbin:/bin:/sbin",
        "--setenv", "TERM", "dumb",
    ]
    return args


def _truncate(data: bytes, limit: int) -> str:
    text = data.decode("utf-8", errors="replace")
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    dropped = len(text) - limit
    return text[:head] + f"\n[...truncated {dropped} chars...]\n" + text[-tail:]


async def _run(command: str, timeout_sec: int, network: bool,
               view: SandboxView) -> str:
    timeout_sec = max(1, min(int(timeout_sec), MAX_TIMEOUT_SEC))
    session_id = SESSION_ID_VAR.get()
    work = _work_dir(session_id)

    if EXEC_MODE != "bwrap":
        # netns mode: delegate to the executor daemon running inside the
        # isolated network namespace.  Truncation, timeout enforcement, and
        # proxy injection are handled by the executor; we just format the
        # result to match the established [exit N]\n<body> contract.
        exit_code, output = await netns_client.run_command(
            command, timeout_sec, env={}, cwd=str(work)
        )
        return f"[exit {exit_code}]\n{output}"

    # bwrap mode (fallback): original bubblewrap sandbox — do not modify.
    opts = _build_bwrap_opts(work, view, network)

    # Options go through an in-memory fd to bypass ARG_MAX (spec §4.3.1); the
    # command tail stays on the real argv (bwrap 0.8.0 requires this).
    fd = os.memfd_create("bwrap-args", 0)
    try:
        payload = b"\0".join(a.encode("utf-8") for a in opts) + b"\0"
        os.write(fd, payload)
        os.lseek(fd, 0, os.SEEK_SET)
        os.set_inheritable(fd, True)
        prefix = [
            PRLIMIT_BIN,
            f"--as={MEM_BYTES}",
            f"--cpu={MAX_TIMEOUT_SEC}",
            f"--nofile={NOFILE}",
            BWRAP_BIN, "--args", str(fd),
            "--", "/bin/bash", "-lc", command,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *prefix,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                pass_fds=(fd,),
            )
        except FileNotFoundError as e:
            return f"sandbox unavailable: {e}"
    finally:
        os.close(fd)

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
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


async def _maybe_grant_network(session_id: str, command: str) -> bool:
    """Return True if the sandbox may use the network for this command."""
    db = DB_VAR.get()
    if db is not None and dbmod.is_network_granted(db, session_id):
        return True
    mgr = CONFIRM_MGR_VAR.get()
    sink = EVENT_QUEUE_VAR.get()
    if mgr is None or sink is None:
        return False  # headless: cannot confirm → stay offline
    confirm_id = mgr.register(session_id, "shell_network",
                              "Agent 请求联网执行命令", command)
    await sink.put({
        "type": "confirmation_required",
        "confirm_id": confirm_id,
        "action": "shell_network",
        "description": "Agent 请求联网执行命令",
        "command": command,
    })
    granted = await mgr.wait(confirm_id)
    if granted and db is not None:
        dbmod.grant_network(db, session_id)
    return bool(granted)


async def _run_command_impl(command: str, timeout_sec: int, network: bool) -> str:
    session_id = SESSION_ID_VAR.get()
    db = DB_VAR.get()
    user_patterns = USER_PATTERNS_VAR.get([])
    view = build_view(session_id, db, user_patterns) if db is not None else SandboxView()

    if EXEC_MODE != "bwrap":
        # netns mode: network is always available (managed by egress proxy/DLP).
        # Skip the _maybe_grant_network confirmation flow entirely.
        # The `network` parameter is accepted for API compatibility but ignored.
        return await _run(command, timeout_sec, False, view)

    # bwrap mode: original network-grant + offline-hint logic — do not modify.
    use_net = await _maybe_grant_network(session_id, command) if network else False
    if network and not use_net:
        return "联网请求被拒绝(用户拒绝或当前会话无法确认)。可去掉 network 离线执行。"

    result = await _run(command, timeout_sec, use_net, view)

    if not use_net and result.startswith("[exit ") and not result.startswith("[exit 0]"):
        result += _NETWORK_HINT
    return result


@function_tool
async def run_command(command: str, timeout_sec: int = DEFAULT_TIMEOUT_SEC,
                      network: bool = False) -> str:
    """Run a bash command inside an isolated execution environment.

    The environment provides:
      - Commands run as root inside an isolated network namespace (netns mode,
        default) or inside a bubblewrap sandbox (bwrap mode, legacy fallback,
        set NIMOOS_AGENT_EXEC_MODE=bwrap).
      - The user's AUTHORIZED folders/files are accessible at their real paths
        (read-only for browsing; use write_file/edit_file/delete_path/batch_fs
        to modify).  Unauthorized paths are not present.
      - A writable /work directory (cwd) that persists across calls within the
        same session.
      - Network is AVAILABLE.  Outbound connections to the public internet are
        subject to egress DLP controls: connections to previously unseen
        domains require one-time confirmation from the user, and large uploads
        may be blocked.  Internal LAN/loopback traffic is unrestricted.

    ``network`` parameter: compatibility reserved.  In netns mode network is
    always available and this flag has no effect.  In bwrap (fallback) mode it
    controls whether internet access is enabled (requires user confirmation once
    per session).

    Result is combined stdout+stderr, truncated to ~16 KiB; first line is
    ``[exit N]`` or ``[killed: timeout Ns]``.  Default timeout 30 s, max 300 s.
    """
    return await _run_command_impl(command, timeout_sec, network)


ALL_TOOLS = [run_command]
