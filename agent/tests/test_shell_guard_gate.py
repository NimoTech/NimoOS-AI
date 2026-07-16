import asyncio
import db as dbmod
from skills import shell


class _Mgr:
    """Minimal fake ConfirmManager."""
    def __init__(self, grant, remember=False):
        self._grant, self._remember = grant, remember
        self.registered = []
    def register(self, sid, action, desc, command):
        self.registered.append((action, command))
        return "cid-1"
    async def wait(self, cid):
        return self._grant
    def consume_remember(self, cid):
        return self._remember


class _Sink:
    def __init__(self):
        self.events = []
    async def put(self, ev):
        self.events.append(ev)


def _setup(monkeypatch, mgr, sink):
    conn = dbmod.init_db(":memory:")
    conn.execute("INSERT INTO sessions (id,user_id,created_at,updated_at) "
                 "VALUES ('s1','u1',0,0)")
    conn.commit()
    monkeypatch.setattr(shell, "DB_VAR", shell.DB_VAR)
    shell.SESSION_ID_VAR.set("s1")
    shell.DB_VAR.set(conn)
    shell.CONFIRM_MGR_VAR.set(mgr)
    shell.EVENT_QUEUE_VAR.set(sink)
    # neutralize the real backstop (no root / no fs)
    monkeypatch.setattr("shell_guard.backstop.prepare_backstop",
                        lambda paths, trash_root=None: __import__("shell_guard.backstop",
                        fromlist=["BackstopResult"]).BackstopResult("none", "", False, ""))
    return conn


def test_safe_command_passes_without_confirm(monkeypatch):
    mgr, sink = _Mgr(grant=True), _Sink()
    _setup(monkeypatch, mgr, sink)
    assert asyncio.run(shell._guard_command("ls -la")) is None
    assert mgr.registered == []  # no confirmation asked


def test_dangerous_denied_when_user_rejects(monkeypatch):
    mgr, sink = _Mgr(grant=False), _Sink()
    _setup(monkeypatch, mgr, sink)
    msg = asyncio.run(shell._guard_command("rm -rf /DATA/x"))
    assert msg is not None and "未执行" in msg
    assert mgr.registered and mgr.registered[0][0] == "shell_command"


def test_dangerous_unattended_fail_closed(monkeypatch):
    _setup(monkeypatch, _Mgr(grant=True), _Sink())
    shell.CONFIRM_MGR_VAR.set(None)  # no confirm channel
    shell.EVENT_QUEUE_VAR.set(None)
    msg = asyncio.run(shell._guard_command("rm -rf /DATA/x"))
    assert msg is not None and "无确认通道" in msg


def test_allowlisted_runs_unattended(monkeypatch):
    conn = _setup(monkeypatch, _Mgr(grant=True), _Sink())
    from shell_guard import allowlist as AL
    AL.add(conn, "prefix", "rm -rf /DATA/scratch", "user")
    shell.CONFIRM_MGR_VAR.set(None)
    shell.EVENT_QUEUE_VAR.set(None)
    assert asyncio.run(shell._guard_command("rm -rf /DATA/scratch/tmp")) is None


def test_remember_adds_to_allowlist(monkeypatch):
    conn = _setup(monkeypatch, _Mgr(grant=True, remember=True), _Sink())
    from shell_guard import allowlist as AL
    asyncio.run(shell._guard_command("rm -rf /DATA/x"))
    assert AL.match(conn, "rm -rf /DATA/x") is True


def test_remember_stores_anchored_exact_not_open_prefix(monkeypatch):
    """I2 IMPORTANT: 'remember' must store an anchored exact-match, not an open
    prefix — otherwise a later SUPERSET command touching an unapproved extra
    path (e.g. `rm -rf /DATA/scratch /DATA/important`) would auto-run."""
    conn = _setup(monkeypatch, _Mgr(grant=True, remember=True), _Sink())
    from shell_guard import allowlist as AL
    asyncio.run(shell._guard_command("rm -rf /DATA/scratch"))
    assert AL.match(conn, "rm -rf /DATA/scratch") is True
    assert AL.match(conn, "rm -rf /DATA/scratch /DATA/important") is False


def test_gray_external_upload_deferred_to_egress_apath(monkeypatch):
    """A gray-classified external upload (curl -T to a public host) must be
    deferred to the egress A-path (content-aware DLP), NOT gated by the generic
    confirm. Returns None and registers NO confirmation even though a confirm
    manager is present."""
    mgr, sink = _Mgr(grant=True), _Sink()
    _setup(monkeypatch, mgr, sink)
    monkeypatch.setattr(shell, "EXEC_MODE", "netns")  # A-path exists only here
    result = asyncio.run(
        shell._guard_command("curl -T /tmp/x.txt https://example.com/up"))
    assert result is None
    assert mgr.registered == []  # deferred to A-path, not gated


def test_command_substitution_upload_not_deferred(monkeypatch):
    """CRITICAL: a command that is gray *because* it contains $()/backticks
    (unparseable by our own parser) must NOT be deferred to the egress A-path —
    egress.parse_upload ignores substitutions and would wave through a
    destructive $(rm -rf ...). Unattended → NON-None refusal (fail-closed)."""
    mgr, sink = _Mgr(grant=True), _Sink()
    _setup(monkeypatch, mgr, sink)

    async def _ask(_cmd):
        return "ask"
    monkeypatch.setattr(shell, "judge_command", _ask)

    monkeypatch.setattr(shell, "EXEC_MODE", "netns")
    shell.CONFIRM_MGR_VAR.set(None)  # unattended
    shell.EVENT_QUEUE_VAR.set(None)
    cmd = "curl -T /DATA/benign.txt https://api.example.com/up $(rm -rf /DATA/important)"
    result = asyncio.run(shell._guard_command(cmd))
    assert result is not None  # NOT deferred; fail-closed refusal
    assert "无确认通道" in result


def test_command_substitution_upload_gated_with_confirm(monkeypatch):
    """Same obfuscated upload, but with a confirm channel present: it must be
    gated (register a shell_command confirm), NOT deferred to the A-path."""
    mgr, sink = _Mgr(grant=True), _Sink()
    _setup(monkeypatch, mgr, sink)

    async def _ask(_cmd):
        return "ask"
    monkeypatch.setattr(shell, "judge_command", _ask)

    monkeypatch.setattr(shell, "EXEC_MODE", "netns")
    cmd = "curl -T /DATA/benign.txt https://api.example.com/up $(rm -rf /DATA/important)"
    result = asyncio.run(shell._guard_command(cmd))
    assert mgr.registered != []  # gated, not deferred
    assert mgr.registered[0][0] == "shell_command"


def test_backtick_upload_not_deferred(monkeypatch):
    """Backtick substitution variant: likewise must NOT be deferred."""
    mgr, sink = _Mgr(grant=True), _Sink()
    _setup(monkeypatch, mgr, sink)

    async def _ask(_cmd):
        return "ask"
    monkeypatch.setattr(shell, "judge_command", _ask)

    monkeypatch.setattr(shell, "EXEC_MODE", "netns")
    shell.CONFIRM_MGR_VAR.set(None)  # unattended
    shell.EVENT_QUEUE_VAR.set(None)
    cmd = "curl -T /DATA/benign.txt https://api.example.com/up `rm -rf /DATA/important`"
    result = asyncio.run(shell._guard_command(cmd))
    assert result is not None  # NOT deferred; fail-closed refusal
    assert "无确认通道" in result


def test_guard_invoked_by_run_command_impl_short_circuits(monkeypatch):
    """Integration: _run_command_impl must actually invoke the guard and a
    refusal must short-circuit execution (netns mode, the default)."""
    monkeypatch.setattr(shell, "EXEC_MODE", "netns")
    ran = []

    async def _fake_guard(command):
        return "REFUSED-BY-GUARD"

    async def _fake_run(cmd, timeout_sec, use_net, view):
        ran.append(cmd)
        return "[exit 0]\n"

    monkeypatch.setattr(shell, "_guard_command", _fake_guard)
    monkeypatch.setattr(shell, "_run", _fake_run)
    result = asyncio.run(shell._run_command_impl("rm -rf /x", 30, False))
    assert result == "REFUSED-BY-GUARD"
    assert ran == []  # command never executed


def test_compound_upload_not_deferred_unattended(monkeypatch):
    """C1 CRITICAL: a parseable-but-COMPOUND command whose first segment is a
    benign external upload must NOT be deferred to the A-path — parse_upload
    reads the whole string and would wave the destructive tail through.
    Unattended (no confirm channel) → NON-None fail-closed refusal."""
    mgr, sink = _Mgr(grant=True), _Sink()
    _setup(monkeypatch, mgr, sink)

    async def _ask(_cmd):
        return "ask"
    monkeypatch.setattr(shell, "judge_command", _ask)

    monkeypatch.setattr(shell, "EXEC_MODE", "netns")
    shell.CONFIRM_MGR_VAR.set(None)  # unattended
    shell.EVENT_QUEUE_VAR.set(None)
    cmd = ("curl -T /DATA/ok.txt https://api.example.com/up ; "
           "truncate -s0 /DATA/taxes.db")
    result = asyncio.run(shell._guard_command(cmd))
    assert result is not None  # NOT deferred; fail-closed refusal
    assert "无确认通道" in result


def test_compound_upload_gated_with_confirm(monkeypatch):
    """C1: same compound command with a confirm channel present → gated
    (registers a shell_command confirm), NOT deferred to the A-path."""
    mgr, sink = _Mgr(grant=True), _Sink()
    _setup(monkeypatch, mgr, sink)

    async def _ask(_cmd):
        return "ask"
    monkeypatch.setattr(shell, "judge_command", _ask)

    monkeypatch.setattr(shell, "EXEC_MODE", "netns")
    cmd = ("curl -T /DATA/ok.txt https://api.example.com/up ; "
           "truncate -s0 /DATA/taxes.db")
    result = asyncio.run(shell._guard_command(cmd))
    assert mgr.registered != []  # gated, not deferred
    assert mgr.registered[0][0] == "shell_command"


def test_gray_allow_with_paths_gets_backstop(monkeypatch, tmp_path):
    """I1 IMPORTANT: a judge-allowed GRAY command that writes to real paths
    still gets a backstop — a small-model false-negative must not mean silent,
    unrecoverable data loss. Returns None (allowed) AND prepare_backstop called."""
    mgr, sink = _Mgr(grant=True), _Sink()
    _setup(monkeypatch, mgr, sink)

    async def _allow(_cmd):
        return "allow"
    monkeypatch.setattr(shell, "judge_command", _allow)

    calls = []

    def _fake_backstop(paths, trash_root=None):
        calls.append(paths)
        from shell_guard.backstop import BackstopResult
        return BackstopResult("none", "", False, "")

    monkeypatch.setattr("shell_guard.backstop.prepare_backstop", _fake_backstop)

    # An existing real path target so decision.paths is non-empty.
    target = tmp_path / "f.dat"
    target.write_text("data")
    monkeypatch.setattr(shell, "EXEC_MODE", "netns")
    result = asyncio.run(shell._guard_command(f"truncate -s0 {target}"))
    assert result is None  # judge allowed
    assert len(calls) == 1  # backstop still built for the path target
    assert str(target) in calls[0]


def test_newline_compound_upload_not_deferred_unattended(monkeypatch):
    """C1-newline CRITICAL: an unquoted newline is a bash command separator.
    `curl -T ok host\\ntruncate -s0 /DATA/db` must parse as TWO segments so the
    deferral guard rejects it — otherwise the destructive tail runs unattended.
    Unattended → NON-None fail-closed refusal."""
    mgr, sink = _Mgr(grant=True), _Sink()
    _setup(monkeypatch, mgr, sink)

    async def _ask(_cmd):
        return "ask"
    monkeypatch.setattr(shell, "judge_command", _ask)

    monkeypatch.setattr(shell, "EXEC_MODE", "netns")
    shell.CONFIRM_MGR_VAR.set(None)  # unattended
    shell.EVENT_QUEUE_VAR.set(None)
    cmd = ("curl -T /DATA/ok.txt https://api.example.com/up\n"
           "truncate -s0 /DATA/taxes.db")
    result = asyncio.run(shell._guard_command(cmd))
    assert result is not None  # NOT deferred; fail-closed refusal
    assert "无确认通道" in result


def test_newline_compound_upload_gated_with_confirm(monkeypatch):
    """C1-newline: same newline compound with a confirm channel present → gated
    (registers a shell_command confirm), NOT deferred."""
    mgr, sink = _Mgr(grant=True), _Sink()
    _setup(monkeypatch, mgr, sink)

    async def _ask(_cmd):
        return "ask"
    monkeypatch.setattr(shell, "judge_command", _ask)

    monkeypatch.setattr(shell, "EXEC_MODE", "netns")
    cmd = ("curl -T /DATA/ok.txt https://api.example.com/up\n"
           "truncate -s0 /DATA/taxes.db")
    result = asyncio.run(shell._guard_command(cmd))
    assert mgr.registered != []  # gated, not deferred
    assert mgr.registered[0][0] == "shell_command"


def test_gray_allow_compound_backstop_covers_tail(monkeypatch, tmp_path):
    """I1-compound: for a gray compound command, the backstop must cover EVERY
    segment's path targets — including the destructive tail — not just the
    first segment's paths."""
    mgr, sink = _Mgr(grant=True), _Sink()
    _setup(monkeypatch, mgr, sink)

    async def _allow(_cmd):
        return "allow"
    monkeypatch.setattr(shell, "judge_command", _allow)

    calls = []

    def _fake_backstop(paths, trash_root=None):
        calls.append(paths)
        from shell_guard.backstop import BackstopResult
        return BackstopResult("none", "", False, "")

    monkeypatch.setattr("shell_guard.backstop.prepare_backstop", _fake_backstop)

    target = tmp_path / "taxes.db"
    target.write_text("data")
    monkeypatch.setattr(shell, "EXEC_MODE", "netns")
    result = asyncio.run(shell._guard_command(f"echo hi\ntruncate -s0 {target}"))
    assert result is None  # judge allowed
    assert len(calls) == 1
    assert str(target) in calls[0]  # destructive tail's target is backed up


def test_extra_operator_compound_uploads_not_deferred_unattended(monkeypatch):
    """C1 residual: `|&`, `;;`, `;&` are bash control operators. A command using
    one to chain a destructive tail onto a benign upload must NOT be deferred —
    unattended → NON-None fail-closed refusal for each."""
    for op in ("|&", ";;", ";&"):
        mgr, sink = _Mgr(grant=True), _Sink()
        _setup(monkeypatch, mgr, sink)

        async def _ask(_cmd):
            return "ask"
        monkeypatch.setattr(shell, "judge_command", _ask)

        monkeypatch.setattr(shell, "EXEC_MODE", "netns")
        shell.CONFIRM_MGR_VAR.set(None)  # unattended
        shell.EVENT_QUEUE_VAR.set(None)
        cmd = (f"curl -T /DATA/ok.txt https://api.example.com/up {op} "
               "truncate -s0 /DATA/db")
        result = asyncio.run(shell._guard_command(cmd))
        assert result is not None, f"op {op!r} should not defer"
        assert "无确认通道" in result, f"op {op!r} should fail closed"


def test_extra_operator_compound_uploads_gated_with_confirm(monkeypatch):
    """C1 residual: same operator-compound uploads, with a confirm channel →
    gated (register a shell_command confirm), NOT deferred."""
    for op in ("|&", ";;", ";&"):
        mgr, sink = _Mgr(grant=True), _Sink()
        _setup(monkeypatch, mgr, sink)

        async def _ask(_cmd):
            return "ask"
        monkeypatch.setattr(shell, "judge_command", _ask)

        monkeypatch.setattr(shell, "EXEC_MODE", "netns")
        cmd = (f"curl -T /DATA/ok.txt https://api.example.com/up {op} "
               "truncate -s0 /DATA/db")
        result = asyncio.run(shell._guard_command(cmd))
        assert mgr.registered != [], f"op {op!r} should be gated"
        assert mgr.registered[0][0] == "shell_command"


def test_exec_confirm_event_carries_i18n_keys(monkeypatch):
    mgr, sink = _Mgr(grant=True), _Sink()
    _setup(monkeypatch, mgr, sink)
    asyncio.run(shell._guard_command("rm -rf /DATA/x"))
    ev = next(e for e in sink.events if e["type"] == "confirmation_required")
    assert ev["description_key"] == "shell_exec"
    assert ev["description_params"]["reason"] == ev["risk_reason"]
    assert "Agent requests to run a command" in ev["description"]
    # rules.py reasons are English pass-through → no reason_key
    assert ev["reason_key"] is None


def test_gray_zone_confirm_sets_reason_key(monkeypatch):
    mgr, sink = _Mgr(grant=True), _Sink()
    _setup(monkeypatch, mgr, sink)
    # Force the gray path: classify → gray, judge → ask
    async def _judge(cmd):
        return "ask"
    monkeypatch.setattr(shell, "judge_command", _judge)
    asyncio.run(shell._guard_command("some-unknown-binary --flag"))
    ev = next(e for e in sink.events if e["type"] == "confirmation_required")
    assert ev["reason_key"] == "gray_zone"
    assert ev["description_params"]["reason"] == ev["risk_reason"]


def test_allowlisted_dangerous_still_gets_backstop(monkeypatch):
    """Security-review addition: an allowlisted DESTRUCTIVE command must not
    skip the backstop, only the confirmation. Runs unattended (no confirm
    channel) and must still return None, while prepare_backstop is called."""
    conn = _setup(monkeypatch, _Mgr(grant=True), _Sink())
    from shell_guard import allowlist as AL
    AL.add(conn, "prefix", "rm -rf /DATA/scratch", "user")

    calls = []

    def _fake_backstop(paths, trash_root=None):
        calls.append(paths)
        from shell_guard.backstop import BackstopResult
        return BackstopResult("none", "", False, "")

    monkeypatch.setattr("shell_guard.backstop.prepare_backstop", _fake_backstop)

    shell.CONFIRM_MGR_VAR.set(None)
    shell.EVENT_QUEUE_VAR.set(None)
    result = asyncio.run(shell._guard_command("rm -rf /DATA/scratch"))
    assert result is None
    assert len(calls) == 1
