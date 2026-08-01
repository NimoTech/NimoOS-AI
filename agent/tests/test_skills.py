import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import skills.app_management as am
from confirm import ConfirmManager

# Inject context vars before calling tools
@pytest.fixture(autouse=True)
def setup_ctx(tmp_path):
    import db as db_module
    conn = db_module.init_db(str(tmp_path / "test.db"))
    queue = asyncio.Queue()
    mgr = ConfirmManager(conn, timeout=1.0)
    am.SESSION_ID_VAR.set("test-session")
    am.EVENT_QUEUE_VAR.set(queue)
    am.CONFIRM_MGR_VAR.set(mgr)
    return queue, mgr

@pytest.mark.asyncio
async def test_list_apps_calls_cli():
    with patch("skills.app_management.run_cli", new_callable=AsyncMock, return_value="[app1]") as mock:
        result = await am.list_apps.on_invoke_tool(MagicMock(), "")
        mock.assert_called_once_with([am.CLI_BIN, "app-management", "list", "apps"])
        assert "app1" in result

@pytest.mark.asyncio
async def test_search_apps_validates_query():
    with patch("skills.app_management.run_cli", new_callable=AsyncMock):
        result = await am.search_apps.on_invoke_tool(MagicMock(), '{"query": "bad; rm -rf /"}')
        assert "Invalid" in result or "Error" in result

@pytest.mark.asyncio
async def test_stop_app_emits_confirmation_event(setup_ctx):
    queue, mgr = setup_ctx
    captured = {}

    async def driver():
        evt = await queue.get()
        captured["event"] = evt
        mgr.resolve(evt["confirm_id"], confirmed=True)

    asyncio.create_task(driver())
    with patch("skills.app_management.run_cli", new_callable=AsyncMock, return_value="stopped"):
        await am.stop_app.on_invoke_tool(MagicMock(), '{"app_id": "42"}')
    event = captured["event"]
    assert event["type"] == "confirmation_required"
    assert event["action"] == "stop_app"
    assert event["confirm_id"]  # non-empty

import skills.storage as st
import skills.healthcheck as hc
import skills.message_bus as mb

@pytest.mark.asyncio
async def test_list_storage_calls_cli():
    with patch("skills.storage.run_cli", new_callable=AsyncMock, return_value="disk info") as mock:
        result = await st.list_storage.on_invoke_tool(MagicMock(), "")
        mock.assert_called_once()
        assert "disk info" in result

@pytest.mark.asyncio
async def test_check_services_calls_cli():
    with patch("skills.healthcheck.run_cli", new_callable=AsyncMock, return_value="all OK") as mock:
        result = await hc.check_services.on_invoke_tool(MagicMock(), "")
        mock.assert_called_once()
        assert "all OK" in result

@pytest.mark.asyncio
async def test_trigger_action_emits_confirm(setup_ctx):
    queue, mgr = setup_ctx
    mb.SESSION_ID_VAR.set("test-session")
    mb.EVENT_QUEUE_VAR.set(queue)
    mb.CONFIRM_MGR_VAR.set(mgr)
    captured = {}

    async def driver():
        evt = await queue.get()
        captured["event"] = evt
        mgr.resolve(evt["confirm_id"], confirmed=False)

    asyncio.create_task(driver())
    with patch("skills.message_bus.run_cli", new_callable=AsyncMock):
        result = await mb.trigger_action.on_invoke_tool(MagicMock(), '{"action_type": "backup.start", "data": "{}"}')
    assert "cancelled" in result.lower()
    event = captured["event"]
    assert event["type"] == "confirmation_required"
    assert event["confirm_id"]


import shutil
import skills.shell as sh


def test_shell_argv_shape(tmp_path):
    from fs.sandbox_view import SandboxView
    work = tmp_path / "work"
    work.mkdir()
    opts = sh._build_bwrap_opts(work, SandboxView(), network=False)
    # opts are bwrap OPTIONS only (no prlimit, no bwrap bin, no command tail)
    assert "--bind" in opts and str(work) in opts and "/work" in opts
    assert "--unshare-all" in opts
    assert "--unshare-net" in opts        # offline by default now
    assert "--share-net" not in opts
    assert "--die-with-parent" in opts
    assert "/bin/bash" not in opts        # command lives on the real argv


def test_shell_truncate():
    assert sh._truncate(b"hello", 100) == "hello"
    big = ("x" * 50_000).encode()
    out = sh._truncate(big, 1000)
    assert "truncated" in out
    assert len(out) < 1500  # roughly limit + a marker


@pytest.mark.asyncio
async def test_shell_run_command_smoke(tmp_path, monkeypatch):
    if not shutil.which("bwrap"):
        pytest.skip("bwrap not installed")
    monkeypatch.setenv("NIMOOS_AGENT_SHELL_ROOT", str(tmp_path))
    sh.WORK_ROOT = tmp_path  # module already read env at import time
    sh.SESSION_ID_VAR.set("smoke")
    out = await sh.run_command.on_invoke_tool(
        MagicMock(),
        '{"command": "echo hello && pwd && id -u"}',
    )
    assert "[exit 0]" in out
    assert "hello" in out
    assert "/work" in out


@pytest.mark.asyncio
async def test_shell_isolation_etc_readonly(tmp_path):
    if not shutil.which("bwrap"):
        pytest.skip("bwrap not installed")
    sh.WORK_ROOT = tmp_path
    sh.SESSION_ID_VAR.set("iso")
    out = await sh.run_command.on_invoke_tool(
        MagicMock(),
        '{"command": "touch /etc/should_fail 2>&1; echo done"}',
    )
    assert "done" in out
    assert "Read-only" in out or "Permission" in out


@pytest.mark.asyncio
async def test_shell_persists_within_session(tmp_path):
    if not shutil.which("bwrap"):
        pytest.skip("bwrap not installed")
    sh.WORK_ROOT = tmp_path
    sh.SESSION_ID_VAR.set("persist")
    await sh.run_command.on_invoke_tool(
        MagicMock(), '{"command": "echo state > marker.txt"}'
    )
    out = await sh.run_command.on_invoke_tool(
        MagicMock(), '{"command": "cat marker.txt"}'
    )
    assert "state" in out


@pytest.mark.asyncio
async def test_shell_timeout_kills():
    if not shutil.which("bwrap"):
        pytest.skip("bwrap not installed")
    sh.SESSION_ID_VAR.set("timeout")
    out = await sh.run_command.on_invoke_tool(
        MagicMock(), '{"command": "sleep 5", "timeout_sec": 1}'
    )
    assert "killed" in out
