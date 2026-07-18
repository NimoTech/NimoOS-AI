import sqlite3
import pytest
import db as dbmod
from skills import shell


class _Sink:
    def __init__(self): self.events = []
    async def put(self, e): self.events.append(e)


class _Mgr:
    """Fake ConfirmManager: resolves wait() with a preset decision."""
    def __init__(self, decision): self.decision = decision; self.registered = []
    def register(self, session_id, action, description, command):
        self.registered.append((action, command)); return "cid"
    async def wait(self, cid): return self.decision


@pytest.fixture(autouse=True)
def _neutralize_guard(monkeypatch):
    """Neutralize the L1 command guardrail for this suite. These tests verify
    the bwrap network-grant / offline-hint logic in _run_command_impl, which
    runs after the guard; the guard is unit-tested in test_shell_guard_gate.py.
    Without this, the guard would classify the placeholder `curl x` commands as
    gray and gate them before the network-grant logic under test is reached."""
    async def _passthrough_guard(command):
        return None
    monkeypatch.setattr(shell, "_guard_command", _passthrough_guard)


def _mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT, "
                 "title TEXT, created_at INT, updated_at INT, "
                 "network_granted INTEGER NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO sessions VALUES ('s1','u',NULL,0,0,0)")
    conn.execute("CREATE TABLE visible_resources (session_id TEXT, path TEXT, kind TEXT)")
    conn.commit()
    return conn


@pytest.mark.asyncio
async def test_network_denied_returns_message_without_running(monkeypatch):
    conn = _mem_db()
    monkeypatch.setattr(shell, "EXEC_MODE", "bwrap")
    shell.SESSION_ID_VAR.set("s1"); shell.DB_VAR.set(conn)
    shell.CONFIRM_MGR_VAR.set(_Mgr(False)); shell.EVENT_QUEUE_VAR.set(_Sink())
    called = False
    async def _fake_run(cmd, t, net, view):
        nonlocal called; called = True; return "[exit 0]\n"
    monkeypatch.setattr(shell, "_run", _fake_run)
    out = await shell._run_command_impl("curl x", 30, True)
    assert "denied" in out
    assert called is False


@pytest.mark.asyncio
async def test_network_granted_persists_for_session(monkeypatch):
    conn = _mem_db()
    monkeypatch.setattr(shell, "EXEC_MODE", "bwrap")
    shell.SESSION_ID_VAR.set("s1"); shell.DB_VAR.set(conn)
    mgr = _Mgr(True); shell.CONFIRM_MGR_VAR.set(mgr); shell.EVENT_QUEUE_VAR.set(_Sink())
    seen_net = []
    async def _fake_run(cmd, t, net, view):
        seen_net.append(net); return "[exit 0]\n"
    monkeypatch.setattr(shell, "_run", _fake_run)
    await shell._run_command_impl("curl x", 30, True)
    assert dbmod.is_network_granted(conn, "s1") is True
    mgr.decision = False                      # would deny if asked again
    await shell._run_command_impl("curl y", 30, True)
    assert seen_net == [True, True]           # 2nd call still ran with net
    assert len(mgr.registered) == 1           # only asked once


@pytest.mark.asyncio
async def test_confirm_event_emitted(monkeypatch):
    conn = _mem_db()
    monkeypatch.setattr(shell, "EXEC_MODE", "bwrap")
    shell.SESSION_ID_VAR.set("s1"); shell.DB_VAR.set(conn)
    sink = _Sink(); shell.CONFIRM_MGR_VAR.set(_Mgr(True)); shell.EVENT_QUEUE_VAR.set(sink)
    async def _fake_run(cmd, t, net, view): return "[exit 0]\n"
    monkeypatch.setattr(shell, "_run", _fake_run)
    await shell._run_command_impl("curl x", 30, True)
    ev = next(e for e in sink.events if e.get("type") == "confirmation_required")
    assert ev["description_key"] == "shell_network"
    assert "network access" in ev["description"]


@pytest.mark.asyncio
async def test_offline_failure_appends_hint(monkeypatch):
    conn = _mem_db()
    monkeypatch.setattr(shell, "EXEC_MODE", "bwrap")
    shell.SESSION_ID_VAR.set("s1"); shell.DB_VAR.set(conn)
    async def _fake_run(cmd, t, net, view): return "[exit 6]\ncurl: could not resolve host"
    monkeypatch.setattr(shell, "_run", _fake_run)
    out = await shell._run_command_impl("curl x", 30, False)
    assert "network=true" in out


@pytest.mark.asyncio
async def test_offline_success_no_hint(monkeypatch):
    conn = _mem_db()
    shell.SESSION_ID_VAR.set("s1"); shell.DB_VAR.set(conn)
    async def _fake_run(cmd, t, net, view): return "[exit 0]\nhello"
    monkeypatch.setattr(shell, "_run", _fake_run)
    out = await shell._run_command_impl("echo hello", 30, False)
    assert "System Hint" not in out


@pytest.mark.asyncio
async def test_timeout_result_no_network_hint(monkeypatch):
    # A timeout kill is NOT a network failure → must not get the network hint.
    conn = _mem_db()
    shell.SESSION_ID_VAR.set("s1"); shell.DB_VAR.set(conn)
    async def _fake_run(cmd, t, net, view): return "[killed: timeout 30s]\n"
    monkeypatch.setattr(shell, "_run", _fake_run)
    out = await shell._run_command_impl("sleep 999", 30, False)
    assert "network=true" not in out
