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
