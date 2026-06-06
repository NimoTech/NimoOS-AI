"""Sandboxed shell tool surface for the agent.

Each ``run_command`` spawns a fresh ``bwrap`` (bubblewrap) subprocess. The
container has read-only system dirs, a session-persistent ``/work``, ``/tmp``
as tmpfs, and — when the user has authorized resources — those folders/files
mounted READ-ONLY at their real paths (with blacklisted subpaths masked).

Network is OFF by default (``--unshare-net``); ``network=True`` asks the user
to confirm, and once granted stays on for the session. bwrap args are passed
via ``--args <fd>`` (an in-memory memfd) to bypass ARG_MAX and avoid pipe
deadlocks.
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


SESSION_ID_VAR: ContextVar[str] = ContextVar("shell_session_id", default="_default")
SANDBOX_SKILLS_VAR: ContextVar[str] = ContextVar("sandbox_skills", default="")
SANDBOX_SHELL_ROOT_VAR: ContextVar[str] = ContextVar("sandbox_shell_root", default="")
# Set by agent.py::run() before every agent loop (mirror the filesystem skill).
DB_VAR: ContextVar = ContextVar("shell_db", default=None)
USER_PATTERNS_VAR: ContextVar[list] = ContextVar("shell_user_patterns", default=[])
CONFIRM_MGR_VAR: ContextVar = ContextVar("shell_confirm_mgr", default=None)
EVENT_QUEUE_VAR: ContextVar = ContextVar("shell_event_queue", default=None)

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
    """Run a bash command inside an isolated sandbox (bubblewrap).

    The sandbox provides:
      - read-only /usr,/etc,/lib (no host $HOME, no other services' data)
      - the user's AUTHORIZED folders/files mounted READ-ONLY at their real
        paths — you can `ls`/`cat`/`grep` them, but NOT modify or delete. To
        change or delete files use write_file/edit_file/delete_path/batch_fs.
        Build/test commands that write caches (pytest -> __pycache__, npm ->
        node_modules) fail with EROFS; copy code into /work to run them.
      - a writable /work directory (cwd; HOME=/work) that persists across calls
      - /tmp as tmpfs
      - NO network by default. Pass network=true to request internet (curl/git/
        apt/pip); the user is asked to confirm once per session.

    Paths the user hasn't authorized are not present (ls -> No such file). If a
    large folder was too big to mount, use glob_files/search instead.

    Result is combined stdout+stderr, truncated to ~16 KiB; first line is
    `[exit N]` or `[killed: timeout Ns]`. Default timeout 30s, max 300s.
    """
    return await _run_command_impl(command, timeout_sec, network)


ALL_TOOLS = [run_command]
