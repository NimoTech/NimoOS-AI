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

import shell_guard
from shell_guard import allowlist as guard_allowlist
from shell_guard import backstop as guard_backstop
from shell_guard.judge import judge_command

# netns_client is imported lazily inside _run() to allow bwrap fallback to load
# and operate even when the netns package is unavailable (e.g. during tests or
# on systems without the executor installed).  Do NOT import it here.


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
               view: SandboxView | None) -> str:
    timeout_sec = max(1, min(int(timeout_sec), MAX_TIMEOUT_SEC))
    session_id = SESSION_ID_VAR.get()
    work = _work_dir(session_id)

    if EXEC_MODE != "bwrap":
        # netns mode: delegate to the executor daemon running inside the
        # isolated network namespace.  Truncation, timeout enforcement, and
        # proxy injection are handled by the executor; we just format the
        # result to match the established [exit N]\n<body> contract.
        # Lazy import: keeps bwrap mode loadable even if netns package is absent.
        from netns import client as netns_client  # noqa: PLC0415
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


async def _guard_command(command: str) -> str | None:
    """Classify `command`; return a refusal string to block, or None to allow.

    SAFE → allow. Allowlisted → allow (even unattended). DANGEROUS/PROTECTED, or
    GRAY judged 'ask' → confirm; unattended without allowlist → fail-closed.
    Before executing a confirmed destructive command, build the backstop.
    """
    session_id = SESSION_ID_VAR.get()
    db = DB_VAR.get()

    decision = shell_guard.classify(command)
    if decision.level == "safe":
        return None

    # user-maintained allowlist wins (runs even unattended)
    if db is not None and guard_allowlist.match(db, command):
        # Allowlisted: skip confirmation, but still build the backstop for
        # destructive commands (defense in depth — a pre-approved rm still
        # gets a recoverable snapshot/trash).
        if decision.level in ("dangerous", "protected"):
            guard_backstop.prepare_backstop(decision.paths)
        return None

    # gray → judge; allow verdict passes through, else fall to confirm
    if decision.level == "gray":
        # Outbound uploads are owned by the egress A-path (content-aware DLP,
        # judge, grant). The generic gate must not preempt or double-confirm
        # them. The A-path only exists in netns mode, so scope the deferral to
        # netns — in bwrap mode there is nothing to defer to, so the command
        # still goes through judge/confirm here. Pipe-to-shell / protected-path
        # uploads are classified above gray and are unaffected by this deferral.
        if EXEC_MODE != "bwrap":
            # Defer benign external uploads to the egress A-path — but ONLY when
            # the command is cleanly parseable by our OWN parser. A command that
            # is gray *because* it contains $()/backticks (segments()->None) must
            # NOT be deferred: egress.parse_upload uses plain shlex.split and
            # ignores substitutions, so it would wave through a destructive
            # $(rm -rf ...). Those fall through to judge/confirm.
            from shell_guard.parse import segments as _seg  # noqa: PLC0415
            if _seg(command) is not None:
                try:
                    from egress import parse as _ep  # noqa: PLC0415
                    _intent = _ep.parse_upload(command)
                    if _intent is not None and _intent.external:
                        return None
                except Exception:  # noqa: BLE001 — parse failure must not block; fall through to judge
                    pass
        verdict = await judge_command(command)
        if verdict == "allow":
            return None
        reason = "命令需人工确认(灰区判定)"
    else:
        reason = decision.reason

    mgr = CONFIRM_MGR_VAR.get()
    sink = EVENT_QUEUE_VAR.get()
    if mgr is None or sink is None:
        return ("此命令需人工批准(无确认通道),未执行。"
                "请在面板确认,或将其加入 shell 白名单。")

    # Build the backstop BEFORE asking, so the card can show undo status.
    backstop = guard_backstop.prepare_backstop(decision.paths)
    if backstop.undoable and backstop.kind == "snapshot":
        undo = "已快照,可回滚"
    elif backstop.undoable and backstop.kind == "trash":
        undo = "已入回收站,可恢复"
    else:
        undo = "⚠ 此操作无法自动备份,执行后不可撤销"

    confirm_id = mgr.register(session_id, "shell_command",
                              f"Agent 请求执行命令:{reason}", command)
    await sink.put({
        "type": "confirmation_required",
        "confirm_id": confirm_id,
        "action": "shell_command",
        "description": f"Agent 请求执行命令:{reason}",
        "command": command,
        "risk_level": decision.level,
        "risk_reason": reason,
        "undo_status": undo,
    })
    granted = await mgr.wait(confirm_id)
    if not granted:
        return "用户拒绝或未能确认该命令,未执行。"
    if db is not None and mgr.consume_remember(confirm_id):
        guard_allowlist.add(db, "prefix", command, "confirm-card")
    return None


async def _run_command_impl(command: str, timeout_sec: int, network: bool) -> str:
    session_id = SESSION_ID_VAR.get()

    # ── Command guardrail (L1): classify + confirm + backstop, both exec modes ──
    _refusal = await _guard_command(command)
    if _refusal is not None:
        return _refusal

    if EXEC_MODE != "bwrap":
        # netns mode: network is always available (managed by egress proxy/DLP).
        # Skip the _maybe_grant_network confirmation flow and build_view entirely
        # (view is only used by bwrap to mount authorized paths; netns ignores it).
        # The `network` parameter is accepted for API compatibility but ignored.

        # ── A-path: content-judge + grant-ticket before netns upload ─────────
        # Lazy imports: keep bwrap fallback loadable even if egress package is absent.
        from egress import parse as _ep, rules as _er, judge as _ej, grant as _eg  # noqa: PLC0415

        try:
            intent = _ep.parse_upload(command)
            if intent is not None and intent.external:
                v = _er.assess(intent.files, inline_payload=None)

                if v.level == "block":
                    return (
                        "该上传被隐私策略拦截,未执行。"
                        f"原因:{v.reason}。"
                        "如确需外发请人工处理。"
                    )

                elif v.level == "clean":
                    # Small and clean — skip LLM, go straight to grant + execute.
                    pass

                else:  # suspect
                    # Read first file content for LLM judge; fall back to b"" on error.
                    content: bytes = b""
                    if intent.files:
                        try:
                            with open(intent.files[0], "rb") as _fh:
                                content = _fh.read(4096)
                        except OSError:
                            content = b""

                    verdict = await _ej.judge(content, intent.host)

                    if verdict == "block":
                        return (
                            "该上传被内容审查拦截,未执行。"
                            "上传内容被判断为含有敏感/隐私数据。"
                            "如确需外发请人工处理。"
                        )
                    elif verdict == "ask":
                        mgr = CONFIRM_MGR_VAR.get()
                        sink = EVENT_QUEUE_VAR.get()
                        if mgr is None or sink is None:
                            # No confirm channel available — fail safe: refuse.
                            return (
                                "无法确认上传操作(无确认通道),未执行。"
                                "请通过界面确认后重试,或人工处理。"
                            )
                        confirm_id = mgr.register(
                            session_id,
                            "egress_upload",
                            f"Agent 请求上传文件到外部主机 {intent.host}",
                            command,
                        )
                        await sink.put({
                            "type": "confirmation_required",
                            "confirm_id": confirm_id,
                            "action": "egress_upload",
                            "description": f"Agent 请求上传文件到外部主机 {intent.host}",
                            "host": intent.host,
                            "files": intent.files,
                            "reason": v.reason,
                            "command": command,
                        })
                        granted = await mgr.wait(confirm_id)
                        if not granted:
                            return (
                                "用户拒绝或未能确认上传操作,未执行。"
                            )
                    # verdict == "allow" OR user confirmed → fall through to grant + execute

                # Compute byte budget: sum of file sizes + 16 KiB headroom.
                # For inline/no-file uploads default to 1 MiB.
                _INLINE_DEFAULT_BYTES = 1 * 1024 * 1024
                _HEADROOM = 16 * 1024
                if intent.files:
                    total_bytes = _HEADROOM
                    for _fp in intent.files:
                        try:
                            total_bytes += os.path.getsize(_fp)
                        except OSError:
                            total_bytes += _INLINE_DEFAULT_BYTES
                else:
                    total_bytes = _INLINE_DEFAULT_BYTES

                # I1: register_grant is synchronous (urllib, up to 3 s); run in
                # executor so it does not block the async event loop.
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda: _eg.register_grant(
                        intent.host, max_bytes=total_bytes, ttl_sec=60
                    ),
                )
        except Exception as _apath_exc:  # noqa: BLE001 — I2: fail-closed skeleton guard
            # An unexpected error in the A-path evaluation (e.g. pathspec version
            # mismatch, import failure after lazy load, etc.).  Log it and refuse
            # conservatively — never execute an unvetted upload command.
            import logging as _logging  # noqa: PLC0415
            _logging.getLogger("nimoos-agent").warning(
                "shell: A-path evaluation raised unexpectedly for command %r: %s",
                command,
                _apath_exc,
            )
            return (
                "上传操作因内部错误无法评估,未执行,请人工处理。"
            )
        # ── end A-path ────────────────────────────────────────────────────────

        return await _run(command, timeout_sec, False, None)

    db = DB_VAR.get()
    user_patterns = USER_PATTERNS_VAR.get([])
    view = build_view(session_id, db, user_patterns) if db is not None else SandboxView()

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
