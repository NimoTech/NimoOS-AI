"""
Tests for /internal/egress-confirm route (Task 8).

Uses TestClient (sync) + monkeypatching — no root, no real subprocesses.
"""

import asyncio
import importlib
import sys

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers to reload main with a fresh DB
# ---------------------------------------------------------------------------

def _reload_main(tmp_path, monkeypatch):
    """Reload main module with an isolated DB and return (main, TestClient)."""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "test.db"))
    # Prevent startup orchestration from touching real subprocesses/netns
    monkeypatch.setenv("NIMOOS_AGENT_EXEC_MODE", "none")
    for mod in list(sys.modules.keys()):
        if mod in ("main", "agent", "db"):
            del sys.modules[mod]
    import main as m
    importlib.reload(m)
    return m, TestClient(m.app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Tests: /internal/egress-confirm
# ---------------------------------------------------------------------------

def test_egress_confirm_no_active_session(tmp_path, monkeypatch):
    """Fail-closed: no active session → {"allow": false}."""
    m, client = _reload_main(tmp_path, monkeypatch)

    # Ensure no active sinks
    m._runner._active_sinks.clear()
    m._runner._last_active_session = None

    resp = client.post(
        "/internal/egress-confirm",
        json={"host": "evil.com", "bytes": 100000, "reason": "upload_over_threshold"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"allow": False}


def test_egress_confirm_allow(tmp_path, monkeypatch):
    """Active session + confirm_mgr.wait returns True → {"allow": true}."""
    m, client = _reload_main(tmp_path, monkeypatch)

    session_id = "test-session-allow"
    sink_queue: asyncio.Queue = asyncio.Queue()

    # Inject a fake active sink
    m._runner._active_sinks[session_id] = sink_queue
    m._runner._last_active_session = session_id

    # Monkeypatch _confirm_mgr so register returns a known id and wait returns True
    original_register = m._confirm_mgr.register
    original_wait = m._confirm_mgr.wait

    registered_ids = []

    def fake_register(sid, action, description, command):
        cid = original_register(sid, action, description, command)
        registered_ids.append(cid)
        # Immediately resolve so wait() doesn't block
        m._confirm_mgr.resolve(cid, confirmed=True)
        return cid

    monkeypatch.setattr(m._confirm_mgr, "register", fake_register)

    resp = client.post(
        "/internal/egress-confirm",
        json={"host": "example.com", "bytes": 1024, "reason": "test"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"allow": True}

    # Verify sink received confirmation_required event
    assert not sink_queue.empty()
    event = sink_queue.get_nowait()
    assert event["type"] == "confirmation_required"
    assert event["action"] == "egress_confirm"
    assert event["host"] == "example.com"
    assert event["reason"] == "test"
    assert "confirm_id" in event


def test_egress_confirm_deny(tmp_path, monkeypatch):
    """Active session + confirm_mgr.wait returns False → {"allow": false}."""
    m, client = _reload_main(tmp_path, monkeypatch)

    session_id = "test-session-deny"
    sink_queue: asyncio.Queue = asyncio.Queue()

    m._runner._active_sinks[session_id] = sink_queue
    m._runner._last_active_session = session_id

    def fake_register(sid, action, description, command):
        cid = m._confirm_mgr.__class__.register(m._confirm_mgr, sid, action, description, command)
        m._confirm_mgr.resolve(cid, confirmed=False)
        return cid

    monkeypatch.setattr(m._confirm_mgr, "register", fake_register)

    resp = client.post(
        "/internal/egress-confirm",
        json={"host": "evil.com", "bytes": 100000, "reason": "upload_over_threshold"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"allow": False}

    # Sink should have received the event regardless of denial
    assert not sink_queue.empty()
    event = sink_queue.get_nowait()
    assert event["type"] == "confirmation_required"
    assert event["host"] == "evil.com"


def test_egress_confirm_sink_gone_after_last_active(tmp_path, monkeypatch):
    """last_active_session set but not in _active_sinks and no other sinks → fail-closed."""
    m, client = _reload_main(tmp_path, monkeypatch)

    m._runner._active_sinks.clear()
    m._runner._last_active_session = "stale-session-id"

    resp = client.post(
        "/internal/egress-confirm",
        json={"host": "evil.com", "bytes": 1, "reason": "test"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"allow": False}


# ---------------------------------------------------------------------------
# Tests: startup orchestration pure functions
# ---------------------------------------------------------------------------

def test_build_proxy_argv_defaults(tmp_path, monkeypatch):
    """_build_proxy_argv constructs the expected argv with defaults."""
    m, _ = _reload_main(tmp_path, monkeypatch)

    argv = m._build_proxy_argv("/usr/local/bin/egress-proxy")
    assert argv[0] == "/usr/local/bin/egress-proxy"
    assert "-listen" in argv
    assert "169.254.7.1:8888" in argv
    assert "-confirm-url" in argv
    confirm_idx = argv.index("-confirm-url")
    assert "egress-confirm" in argv[confirm_idx + 1]
    assert "-grant-listen" in argv


def test_build_proxy_argv_custom(tmp_path, monkeypatch):
    """_build_proxy_argv respects custom parameters."""
    m, _ = _reload_main(tmp_path, monkeypatch)

    argv = m._build_proxy_argv(
        "/custom/proxy",
        listen="0.0.0.0:9999",
        confirm_url="http://localhost:1234/cb",
    )
    assert argv[0] == "/custom/proxy"
    listen_idx = argv.index("-listen")
    assert argv[listen_idx + 1] == "0.0.0.0:9999"


def test_wait_for_pid_file_success(tmp_path, monkeypatch):
    """_wait_for_pid_file returns the PID when file exists."""
    m, _ = _reload_main(tmp_path, monkeypatch)

    pid_file = tmp_path / "test.pid"
    pid_file.write_text("12345\n")

    result = m._wait_for_pid_file(str(pid_file), timeout=1.0)
    assert result == 12345


def test_wait_for_pid_file_timeout(tmp_path, monkeypatch):
    """_wait_for_pid_file raises TimeoutError when file never appears."""
    m, _ = _reload_main(tmp_path, monkeypatch)

    with pytest.raises(TimeoutError):
        m._wait_for_pid_file(str(tmp_path / "nonexistent.pid"), timeout=0.1)


def test_startup_orchestration_popen_failure_does_not_raise(tmp_path, monkeypatch):
    """If Popen raises (no executor binary), egress startup swallows the error."""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "test.db"))
    # Enable netns mode so the startup handler actually runs
    monkeypatch.setenv("NIMOOS_AGENT_EXEC_MODE", "netns")

    for mod in list(sys.modules.keys()):
        if mod in ("main", "agent", "db"):
            del sys.modules[mod]

    import main as m

    original_popen = m.subprocess.Popen

    def failing_popen(*args, **kwargs):
        raise FileNotFoundError("no such file: python executor")

    monkeypatch.setattr(m.subprocess, "Popen", failing_popen)

    # lifespan startup should NOT raise even when Popen fails.
    # Use asyncio.run() (not get_event_loop()) so this works regardless of
    # whether a previous test already closed the default event loop.
    import asyncio
    try:
        asyncio.run(m._egress_startup())
    except Exception as exc:
        pytest.fail(f"_egress_startup raised unexpectedly: {exc}")

    # Agent is still functional — healthz still reachable
    client = TestClient(m.app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
