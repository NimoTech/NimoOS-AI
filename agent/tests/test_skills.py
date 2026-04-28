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
