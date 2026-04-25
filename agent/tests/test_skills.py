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
    async def auto_confirm():
        await asyncio.sleep(0.05)
        mgr.resolve("test-session", confirmed=True)
    asyncio.create_task(auto_confirm())
    with patch("skills.app_management.run_cli", new_callable=AsyncMock, return_value="stopped"):
        await am.stop_app.on_invoke_tool(MagicMock(), '{"app_id": "42"}')
    event = queue.get_nowait()
    assert event["type"] == "confirmation_required"
    assert event["action"] == "stop_app"
