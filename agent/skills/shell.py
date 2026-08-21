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
import permissions
from audit import audit as _audit
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
# Run-scoped shell allowlist (scheduled tasks' `preauth.shell`).  Unlike the
# persistent `shell_allowlist` table this lives only for the duration of one
# agent run and is NEVER written to the DB.  Set by agent.py::run() from the
# run's pre-authorization; the empty default means "no run-scoped grant" and
# keeps the gate bit-identical to its pre-preauth behavior.  The default is an
# immutable tuple on purpose — a mutable default object is shared by every
# context that never set the var.  Readers must treat it as read-only.
RUN_ALLOWLIST_VAR: ContextVar[tuple | list] = ContextVar(
    "shell_run_allowlist", default=())

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

HOMES_ROOT = Path(os.environ.get("NIMOOS_HOMES_ROOT", "/var/lib/nimoos/ai/homes"))


def _user_home_env() -> dict:
    from skills.skills_registry import USER_ID_VAR
    uid = (USER_ID_VAR.get() or "").strip()
    if not uid or not HOMES_ROOT.is_dir():
        return {}
    home = HOMES_ROOT / uid
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError:
        return {}
    return {"HOME": str(home)}


_NETWORK_HINT = ("\n(System hint: the command may have failed because the sandbox is offline by default. "
                 "If network access is genuinely needed, retry with network=true — the user will be asked to confirm.)")


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
            command, timeout_sec, env=_user_home_env(), cwd=str(work)
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
    if db is not None and permissions.auto_approve(db, "shell_network"):
        _audit("shell_network", session_id=session_id, command=command,
               decision="auto_approved_by_policy")
        dbmod.grant_network(db, session_id)
        return True
    mgr = CONFIRM_MGR_VAR.get()
    sink = EVENT_QUEUE_VAR.get()
    if mgr is None or sink is None:
        return False  # headless: cannot confirm → stay offline
    confirm_id = mgr.register(session_id, "shell_network",
                              "Agent requests network access for a command", command)
    await sink.put({
        "type": "confirmation_required",
        "confirm_id": confirm_id,
        "action": "shell_network",
        "description": "Agent requests network access for a command",
        "description_key": "shell_network",
        "command": command,
    })
    granted = await mgr.wait(confirm_id)
    if granted and db is not None:
        dbmod.grant_network(db, session_id)
    return bool(granted)


_SHELL_META_CHARS = ";|&`()<>\n\r"


def _argv_is_atomic(seg) -> bool:
    """True only if no argv token carries a shell control metacharacter — a
    belt-and-suspenders so the upload deferral can never wave through a compound
    command even if _OPERATORS ever misses a separator."""
    return not any(
        ("$(" in tok) or any(ch in tok for ch in _SHELL_META_CHARS)
        for tok in seg.argv
    )


# Commands whose real work lives in a STRING ARGUMENT the classifier never sees.
# `sh -c 'cat /etc/shadow'` classifies as GRAY (argv is just `sh`, `-c`, and an
# opaque string), so the `protected` exclusion below would not fire and a run
# rule like prefix "sh -c " would wave the payload through unattended. The
# persistent allowlist has the same hole, but that is a human-maintained,
# per-machine decision and is out of scope here (tracked follow-up); a
# run-scoped grant must never be usable as an interpreter escape hatch.
_RUN_INTERPRETERS = {
    "sh", "bash", "zsh", "dash", "ash", "ksh", "busybox",
    "python", "python2", "python3", "perl", "ruby", "php", "lua",
    "node", "nodejs", "deno", "bun", "awk", "gawk", "mawk",
    "pwsh", "powershell", "tclsh", "expect", "Rscript", "osascript",
    # Not interpreters in the language sense, but they all take a command (or a
    # recipe/playbook naming one) and run it, so the argv the classifier sees is
    # never the work that happens.  `xargs` also takes its arguments from stdin,
    # which no static check here can see.
    "xargs", "make", "systemd-run", "ansible-playbook",
    # NOT listed: `env` / `nohup` / `nice` / `timeout` / `sudo` / `ionice` — plain
    # exec wrappers.  Listing them would refuse honest invocations
    # (`env LC_ALL=C sort -c f.txt`, `nice -n 10 rsync …`), and what they wrap is
    # caught anyway by the full-argv scan in _run_allowlist_match.  Note this is
    # NOT because unwrapping reveals it: `_effective_argv` stops at a flag's
    # value, so `nice -n 10 sh -c …` never unwraps to `sh` at all (see the scan's
    # comment).  A stale version of this comment claimed otherwise.
}
# find-family flags that hand a command to another process.  Unambiguous (no
# other common tool spells them), so they are checked on every command.
#
# `-c` / `-e` are NOT checked (review round 2, M2): as everyday flags on
# ordinary tools — `curl -e`, `sed -e`, `sort -c`, `cut -c`, `tar -cf`,
# `git commit -c`, `gcc -c`, `docker run -e`, `jq -e`, `ssh -e`,
# `openssl enc -e` — refusing them broke the only use case this feature has.
#
# That trade is NOT free, and the earlier "no security gain" claim was wrong.
# It gives up coverage of launchers whose argv[0] is not itself an interpreter
# but that still run an arbitrary command through a `-c`-ish option, e.g.
# `git -c core.pager='rm -rf /DATA' log` or `uv run python -c …`.  Mitigation
# today is authoring discipline: a rule should name the whole invocation
# (prefix `git log`), not just the binary (prefix `git `).  FOLLOW-UP: an
# option-aware per-tool deny list (git -c/-C/--exec-path, uv/uvx/npx/pipx run,
# find -printf %h, …) rather than a blanket flag scan.
_RUN_EXEC_FLAGS = {"-exec", "-execdir", "-ok", "-okdir"}


# Interpreters that take a SCRIPT FILE as their first operand. Deliberately a
# separate, narrower set than `_RUN_INTERPRETERS` above: that one answers "does
# this command line hide its payload from `classify`?" and therefore includes
# the command-runner family (`xargs`, `make`, `systemd-run`,
# `ansible-playbook`). Those must NOT appear here — `systemd-run <path>` runs
# the path as a transient unit and escapes the run's sandbox outright, and
# `make <path>` does not execute the file at all, so a "two tokens" shape says
# nothing about what would actually run.
_SCRIPT_INTERPRETERS = {
    "sh", "bash", "zsh", "dash", "ash", "ksh",
    "python", "python2", "python3", "perl", "ruby", "php", "lua",
    "node", "nodejs", "deno", "bun", "Rscript", "osascript",
}

# Run-scoped `scripts` pre-authorization (a scheduled task's
# `preauth.scripts`). Same lifetime and same read-only contract as
# RUN_ALLOWLIST_VAR above: set per run, never persisted.
RUN_SCRIPTS_VAR: ContextVar[tuple | list] = ContextVar(
    "shell_run_scripts", default=())


def run_scripts_would_cover(command: str, scripts, *,
                            cwd: str | None = None) -> bool:
    """Would `scripts` let `command` through for one unattended run?

    Public for the same reason `run_allowlist_would_cover` is: the "adopt this
    denied action" flow has to know whether the rule it is about to write would
    actually change the outcome, and asking the gate beats restating it.
    """
    if not isinstance(command, str) or not command.strip():
        return False
    decision = shell_guard.classify(command, cwd=cwd)
    if decision.level == "safe":
        return True
    token = RUN_SCRIPTS_VAR.set(tuple(scripts or ()))
    try:
        return _run_script_match(command, decision)
    finally:
        RUN_SCRIPTS_VAR.reset(token)


def script_run_target(command: str) -> str:
    """The script path if `command` is exactly `<interpreter> <absolute path>`.

    Returns `""` for anything else. This is a pure SHAPE question — "is this one
    interpreter invoking one pinned file?" — deliberately separate from "may it
    run?", which is `_run_script_match`.

    Callers that need to recognize the shape must use THIS, not
    `run_scripts_would_cover`: that probe answers True for every `safe` command
    regardless of rules (the gate returns before consulting any allowlist), so
    using it as a detector misread `lark-cli mail list --limit 5` as a script run
    and adopted `5` as the script path. Caught by
    test_tasks_endpoints.py::test_from_denied_shell_uses_the_command_head.
    """
    if not isinstance(command, str) or not command.strip():
        return ""
    from shell_guard.parse import segments as _seg  # noqa: PLC0415
    segs = _seg(command)
    if segs is None or len(segs) != 1:
        return ""
    if segs[0].redirect_targets or segs[0].read_targets:
        return ""
    # The RAW argv, never `_effective_argv`: unwrapping `env`/`nice`/`timeout`
    # would let a wrapper carry its own operands into a shape judged as
    # "two tokens".
    argv = list(segs[0].argv or ())
    if len(argv) != 2:
        return ""
    if os.path.basename(argv[0]) not in _SCRIPT_INTERPRETERS:
        return ""
    if not argv[1].startswith("/"):
        return ""
    return argv[1]


def _run_script_match(command: str, decision) -> bool:
    """True if the run's `scripts` pre-authorization vouches for `command`.

    The shape is deliberately the narrowest thing that can still run a script:

        <interpreter> <one exact absolute path from the rules>

    and nothing else — exactly two tokens, no flags, no extra operands, no
    chaining, no redirection, never `protected`.

    Why this is safe where a `python3 ` PREFIX rule is not: a prefix vouches for
    every command that starts with it, including `python3 -c "<anything>"`, so
    the payload is unbounded and invisible. Pinning the exact path makes the
    payload one file the author named and can read — the same trust model as
    allowlisting any other binary. Appending an argument is refused for the same
    reason: an argument is input the person who approved the rule never saw.

    The load-bearing check is `basename(argv[0]) in _SCRIPT_INTERPRETERS`.
    Without it, `rm /DATA/AppData/radar/radar.py` is also "two tokens ending in
    an authorized script", i.e. authorizing a script would authorize deleting
    it — and `curl -T <script> https://…` would exfiltrate it.
    """
    scripts = RUN_SCRIPTS_VAR.get() or ()   # read-only: never mutate in place
    if not scripts:
        return False
    if decision.level == "protected":
        return False
    script = script_run_target(command)
    if not script:
        return False
    return any(isinstance(rule, str) and rule == script for rule in scripts)


def run_allowlist_would_cover(command: str, rules, *, cwd: str | None = None) -> bool:
    """Would granting `rules` for one run actually let `command` through?

    Exists for the "adopt this denied action" flow, which turns a denied
    command into a preauth rule. That generator works on the command's head, so
    for a CHAINED command it produced a rule the gate can never honour (chaining
    is refused outright, whatever the rules say) — the button wrote something
    and changed nothing, with no way for the user to tell.

    Answers by running the real gate with `rules` temporarily in scope, rather
    than restating the gate's conditions here: a second copy of "single simple
    command, no interpreters, never protected" would drift from
    `_run_allowlist_match` the first time either side changed.

    `safe` is True regardless of `rules`: `handle_shell_confirmation` returns
    before consulting any allowlist, so such a command was never gated.
    """
    if not isinstance(command, str) or not command.strip():
        return False
    decision = shell_guard.classify(command, cwd=cwd)
    if decision.level == "safe":
        return True
    token = RUN_ALLOWLIST_VAR.set(tuple(rules or ()))
    try:
        return _run_allowlist_match(command, decision)
    finally:
        # Restore, never just clear: this runs inside a live request, and
        # leaking the probe's rules would grant them to whatever runs next.
        RUN_ALLOWLIST_VAR.reset(token)


def _run_allowlist_match(command: str, decision) -> bool:
    """True if the run-scoped pre-authorization vouches for `command`.

    Deliberately STRICTER than the persistent allowlist:

    * ``protected`` is never covered.  The persistent allowlist *does* pass some
      protected commands (a `path_scope` entry, or a `prefix` entry on a mass
      delete under /DATA, whose paths aren't "protected paths" individually) —
      that is a human-maintained, per-machine decision.  A run-scoped grant
      comes from a scheduled task's stored document and runs with nobody
      watching, so protected always falls through to the refusal path.
    * Interpreters and find-style exec flags are never covered (see
      _RUN_INTERPRETERS): their payload is invisible to `classify`, so the
      protected exclusion above would be trivially bypassable.  Ordinary tools'
      flags are NOT inspected — that only over-refused honest commands.
    * Same anti-smuggling shape as `shell_guard.allowlist.match`: a SINGLE
      simple command, no chaining (`;`/`&&`/`|`/subshells) and no redirection,
      so a benign matched prefix can't vouch for a destructive tail.
    """
    rules = RUN_ALLOWLIST_VAR.get() or ()   # read-only: never mutate in place
    if not rules:
        return False
    if decision.level == "protected":
        return False
    from shell_guard.parse import segments as _seg  # noqa: PLC0415
    segs = _seg(command)
    if segs is None or len(segs) != 1:
        return False
    if segs[0].redirect_targets or segs[0].read_targets:
        return False
    # An interpreter anywhere in the command line disqualifies it — the scan is
    # over EVERY token of the segment as written, not just the command name.
    #
    # Narrower checks were tried and both leaked, because `_effective_argv` only
    # skips tokens starting with `-` (plus one operand for timeout/chroot) and
    # therefore stops unwrapping at a flag's VALUE: `nice -n 10 sh -c …` halts on
    # `10`, so neither the peeled prefix nor the unwrapped argv[0] is ever `sh`.
    # Same for `timeout -s KILL 5 bash -c`, `sudo -u root python3 -c`,
    # `ionice -c 2 python3 -c`, `env -u FOO python3 -c`, and
    # `nice -n 10 xargs rm -rf …`.  Scanning all tokens costs nothing and closes
    # the whole family; the price is refusing a command that merely *mentions* an
    # interpreter name as an argument (`apt install make`), which is a
    # pre-authorization the author can simply phrase differently.
    #
    # STILL NOT COVERED (deliberately, and out of scope here): the same early
    # stop also degrades `classify` itself.  `nice -n 10 rm -rf /DATA` is GRAY
    # while `rm -rf /DATA` is PROTECTED, so the protected exclusion above does
    # not fire and a rule naming it grants it — and `rm` is not an interpreter,
    # so this scan does not catch it either.  The defect is in
    # shell_guard._effective_argv and equally affects the persistent allowlist
    # and the judge path; fixing it there is a separate follow-up.
    from shell_guard.rules import _effective_argv  # noqa: PLC0415
    raw_argv = segs[0].argv
    argv = _effective_argv(raw_argv)
    if not argv:
        return False
    # URL-looking ARGUMENTS are exempt: `basename()` of a URL is just its last
    # path segment, so `curl -s https://open.feishu.cn/node` read as an
    # interpreter named `node` — a silent refusal on the most typical
    # scheduled-task command there is.
    #
    # argv[0] is NEVER exempt, however: it is the thing that actually executes,
    # and POSIX collapses `//` in a path, so `foo://bash` resolves to `foo:/bash`
    # — an attacker with a writable work dir can `mkdir 'foo:'` and symlink
    # `foo:/bash` to the real shell, then pass the whole thing off as a URL.
    # Exempting the command name would hand that straight through.
    scan = [raw_argv[0]] + [tok for tok in raw_argv[1:] if "://" not in tok]
    if any(os.path.basename(tok) in _RUN_INTERPRETERS for tok in scan):
        return False
    if any(tok in _RUN_EXEC_FLAGS for tok in argv[1:]):
        return False
    from tasks import preauth as _preauth  # noqa: PLC0415
    return _preauth.shell_match(rules, command)


async def _guard_command(command: str) -> str | None:
    """Classify `command`; return a refusal string to block, or None to allow.

    SAFE → allow. Allowlisted → allow (even unattended). DANGEROUS/PROTECTED, or
    GRAY judged 'ask' → confirm; unattended without allowlist → fail-closed.
    Before executing a confirmed destructive command, build the backstop.
    """
    session_id = SESSION_ID_VAR.get()
    db = DB_VAR.get()

    def _rec(outcome: str, level: str, reason: str = ""):
        try:
            uid_row = db.execute("SELECT user_id FROM sessions WHERE id=?", (session_id,)).fetchone() if db is not None else None
            _uid = uid_row["user_id"] if uid_row else None
        except Exception:  # noqa: BLE001
            _uid = None
        _audit("shell_command", user_id=_uid, session_id=session_id,
               command=command, level=level, reason=reason, outcome=outcome)

    # Resolve relative paths against the command's REAL execution cwd (the
    # session work dir the executor uses), not the classifier process cwd.
    try:
        _cwd = str(_work_dir(session_id)) if session_id else None
    except Exception:  # noqa: BLE001
        _cwd = None
    decision = shell_guard.classify(command, cwd=_cwd)
    if decision.level == "safe":
        return None

    # user-maintained allowlist wins (runs even unattended); a run-scoped
    # pre-authorization (scheduled tasks) is a second, narrower source of the
    # same waiver — see _run_allowlist_match for how it is stricter.
    _persistent_ok = db is not None and guard_allowlist.match(db, command)
    _run_ok = False if _persistent_ok else _run_allowlist_match(command, decision)
    # `scripts` is a third source of the same waiver, narrower than both: it
    # only ever covers `<interpreter> <one exact pinned path>`. Checked last so
    # the audit reason names the broadest rule that actually applied.
    _script_ok = False if (_persistent_ok or _run_ok) else \
        _run_script_match(command, decision)
    if _persistent_ok or _run_ok or _script_ok:
        # Allowlisted: skip confirmation, but still build the backstop for
        # destructive commands (defense in depth — a pre-approved rm still
        # gets a recoverable snapshot/trash).
        if decision.level in ("dangerous", "protected"):
            guard_backstop.prepare_backstop(decision.paths)
        _rec("allowlisted", decision.level,
             "allowlist" if _persistent_ok
             else ("run-preauth" if _run_ok else "run-preauth-script"))
        return None

    # Global permission policy (admin-configured): auto_gray waives the card
    # for gray commands, auto_all also for dangerous. `protected` is never
    # waived, and non-interactive contexts (tasks/channels) cap this at gray —
    # both enforced inside permissions.auto_approve. A waived destructive
    # command still gets the backstop, same as an allowlisted one.
    if db is not None and permissions.auto_approve(
            db, "shell_command", level=decision.level):
        if decision.paths:
            guard_backstop.prepare_backstop(decision.paths)
        _rec("auto_approved", decision.level, "policy")
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
            # Defer benign external uploads to the egress A-path — ONLY when the
            # command is a SINGLE simple command (one segment, no redirects)
            # whose sole purpose is the upload. A command that is gray *because*
            # it contains $()/backticks (segments()->None) is unparseable and must
            # NOT be deferred: egress.parse_upload uses plain shlex.split and would
            # wave through a destructive $(rm -rf ...). Likewise a parseable-but-
            # COMPOUND command (e.g. `curl -T f host ; rm -rf /DATA`) must NOT
            # defer: parse_upload reads the whole string and would wave the
            # destructive tail through. Those fall through to judge/confirm.
            from shell_guard.parse import segments as _seg  # noqa: PLC0415
            _segs = _seg(command)
            if (_segs is not None and len(_segs) == 1
                    and not _segs[0].redirect_targets and not _segs[0].read_targets
                    and _argv_is_atomic(_segs[0])):
                try:
                    from egress import parse as _ep  # noqa: PLC0415
                    _intent = _ep.parse_upload(command)
                    if _intent is not None and _intent.external:
                        _rec("deferred_upload", "gray", "egress-apath")
                        return None
                except Exception:  # noqa: BLE001 — parse failure must not block; fall through to judge
                    pass
        # The judge can be disabled by policy: a gray command then goes
        # straight to the card (or straight through when the policy check
        # above already waived it) instead of waiting on Ollama.
        if db is not None and not permissions.judge_enabled(db, "shell"):
            verdict = "ask"
        else:
            # Surface the wait: the judge can take up to ~20s on a busy local
            # model, and without these events the user just sees the agent
            # stall. `judging` starts the indicator, `judged` ends it and says
            # what happened (allow → ran without a click; ask → the card that
            # follows explains itself). Best-effort: a UI event must never
            # block or fail the gate.
            _jsink = EVENT_QUEUE_VAR.get()
            if _jsink is not None:
                try:
                    await _jsink.put({"type": "judging", "kind": "shell",
                                      "command": command})
                except Exception:  # noqa: BLE001
                    pass
            verdict = await judge_command(command)
            if _jsink is not None:
                try:
                    await _jsink.put({"type": "judged", "kind": "shell",
                                      "command": command, "verdict": verdict})
                except Exception:  # noqa: BLE001
                    pass
        if verdict == "allow":
            # A judge-allowed gray command that writes to real paths still gets a
            # cheap backstop — a small-model false-negative must not mean silent,
            # unrecoverable data loss.
            if decision.paths:
                guard_backstop.prepare_backstop(decision.paths)
            _rec("allowed_gray", "gray", "judge-allow")
            return None
        reason = "needs manual confirmation (gray-zone verdict)"
        reason_key = "gray_zone"
    else:
        reason = decision.reason
        reason_key = None

    mgr = CONFIRM_MGR_VAR.get()
    sink = EVENT_QUEUE_VAR.get()
    if mgr is None or sink is None:
        _rec("refused_unattended", decision.level, reason)
        return ("This command requires manual approval (no confirmation channel available); it was NOT executed. "
                "Confirm it in the panel, or add it to the shell allowlist.")

    # Build the backstop BEFORE asking, so the card can show undo status.
    backstop = guard_backstop.prepare_backstop(decision.paths)
    if backstop.undoable and backstop.kind == "snapshot":
        undo = "snapshot taken; can be rolled back"
    elif backstop.undoable and backstop.kind == "trash":
        undo = "moved to trash; can be restored"
    else:
        undo = "⚠ cannot be backed up automatically; irreversible once executed"

    confirm_id = mgr.register(session_id, "shell_command",
                              f"Agent requests to run a command: {reason}", command)
    await sink.put({
        "type": "confirmation_required",
        "confirm_id": confirm_id,
        "action": "shell_command",
        "description": f"Agent requests to run a command: {reason}",
        "description_key": "shell_exec",
        "description_params": {"reason": reason},
        "reason_key": reason_key,
        "command": command,
        "risk_level": decision.level,
        "risk_reason": reason,
        "undo_status": undo,
    })
    granted = await mgr.wait(confirm_id)
    if not granted:
        _rec("refused_user", decision.level, reason)
        return "The user denied or failed to confirm the command; it was NOT executed."
    if db is not None and mgr.consume_remember(confirm_id):
        # Store an anchored EXACT-match regex, not an open prefix — otherwise a
        # remembered `rm -rf /DATA/scratch` would also auto-run a later superset
        # `rm -rf /DATA/scratch /DATA/important`, deleting an unapproved path.
        import re as _re  # noqa: PLC0415
        guard_allowlist.add(db, "regex", f"^{_re.escape(command)}$", "confirm-card")
    _rec("confirmed", decision.level, reason)
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
                    _audit("egress_block", session_id=session_id,
                           host=intent.host, files=intent.files,
                           stage="rules", reason=v.reason)
                    return (
                        "This upload was blocked by privacy policy and NOT executed. "
                        f"Reason: {v.reason}. "
                        "If it truly must be sent out, handle it manually."
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

                    # Content judge can be disabled by policy: a suspect
                    # upload then falls to the card (or through, if the upload
                    # gate itself is auto). Hard `block` rules above already
                    # ran and are not affected by this toggle.
                    _pdb = DB_VAR.get()
                    if _pdb is not None and not permissions.judge_enabled(_pdb, "egress"):
                        verdict = "ask"
                    else:
                        # Same visible-wait contract as the shell judge above.
                        _jsink = EVENT_QUEUE_VAR.get()
                        if _jsink is not None:
                            try:
                                await _jsink.put({"type": "judging",
                                                  "kind": "upload",
                                                  "host": intent.host,
                                                  "command": command})
                            except Exception:  # noqa: BLE001
                                pass
                        verdict = await _ej.judge(content, intent.host)
                        if _jsink is not None:
                            try:
                                await _jsink.put({"type": "judged",
                                                  "kind": "upload",
                                                  "host": intent.host,
                                                  "command": command,
                                                  "verdict": verdict})
                            except Exception:  # noqa: BLE001
                                pass

                    if verdict == "block":
                        _audit("egress_block", session_id=session_id,
                               host=intent.host, files=intent.files,
                               stage="judge", reason=v.reason)
                        return (
                            "This upload was blocked by content inspection and NOT executed. "
                            "The upload content was judged to contain sensitive/private data. "
                            "If it truly must be sent out, handle it manually."
                        )
                    elif verdict == "ask" and _pdb is not None and \
                            permissions.auto_approve(_pdb, "egress_upload"):
                        # Policy waives the upload card; the grant below still
                        # bounds the byte budget and the audit records it.
                        _audit("egress_grant", session_id=session_id,
                               host=intent.host, files=intent.files,
                               decision="auto_approved_by_policy")
                    elif verdict == "ask":
                        mgr = CONFIRM_MGR_VAR.get()
                        sink = EVENT_QUEUE_VAR.get()
                        if mgr is None or sink is None:
                            # No confirm channel available — fail safe: refuse.
                            return (
                                "Cannot confirm the upload (no confirmation channel); NOT executed. "
                                "Retry after confirming via the UI, or handle it manually."
                            )
                        confirm_id = mgr.register(
                            session_id,
                            "egress_upload",
                            f"Agent requests to upload files to external host {intent.host}",
                            command,
                        )
                        await sink.put({
                            "type": "confirmation_required",
                            "confirm_id": confirm_id,
                            "action": "egress_upload",
                            "description": f"Agent requests to upload files to external host {intent.host}",
                            "description_key": "egress_upload",
                            "description_params": {"host": intent.host},
                            "host": intent.host,
                            "files": intent.files,
                            "reason": v.reason,
                            "command": command,
                        })
                        granted = await mgr.wait(confirm_id)
                        if not granted:
                            return (
                                "The user denied or failed to confirm the upload; it was NOT executed."
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
                # L4: a granted outbound upload is a security-relevant egress
                # decision — audit it (level records rules clean vs judge-allow).
                _audit("egress_grant", session_id=session_id,
                       host=intent.host, files=intent.files,
                       max_bytes=total_bytes, level=v.level)
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
                "The upload could not be evaluated due to an internal error; NOT executed. Handle it manually."
            )
        # ── end A-path ────────────────────────────────────────────────────────

        return await _run(command, timeout_sec, False, None)

    db = DB_VAR.get()
    user_patterns = USER_PATTERNS_VAR.get([])
    view = build_view(session_id, db, user_patterns) if db is not None else SandboxView()

    # bwrap mode: original network-grant + offline-hint logic — do not modify.
    use_net = await _maybe_grant_network(session_id, command) if network else False
    if network and not use_net:
        return "Network access was denied (user declined or this session cannot confirm). You can drop network=true and run offline."

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
