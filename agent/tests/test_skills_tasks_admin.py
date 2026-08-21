"""create_scheduled_task — M5: the agent can only create DISABLED tasks with
an EMPTY preauth document; authorizing and enabling stay in the UI."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import json

import pytest
from unittest.mock import MagicMock

import db as db_module
from skills import tasks_admin
from skills.skills_registry import USER_ID_VAR


@pytest.fixture
def conn(tmp_path, monkeypatch):
    conn = db_module.init_db(str(tmp_path / "t.db"))
    monkeypatch.setattr(db_module, "get_connection", lambda: conn)
    # Some earlier test may have done `sys.modules.pop("db")` and re-imported
    # (test_main_agent_type.py's client fixture does), leaving a DIFFERENT
    # `db` module object in sys.modules than the one this file imported at
    # collection time. tasks_admin resolves `import db` lazily at call time,
    # so pin OUR (patched) module object back, or the patch above is invisible
    # to the code under test whenever that fixture ran first.
    monkeypatch.setitem(sys.modules, "db", db_module)
    return conn


async def _call(**kw):
    return await tasks_admin._create_scheduled_task_impl(
        name=kw.get("name", ""), prompt=kw.get("prompt", ""),
        cron_expr=kw.get("cron_expr", ""),
        interval_seconds=kw.get("interval_seconds", 0))


@pytest.mark.asyncio
async def test_creates_disabled_task_with_empty_preauth(conn):
    USER_ID_VAR.set("u1")
    msg = await _call(name="Daily digest", prompt="do the thing",
                      cron_expr="0 9 * * *")
    row = conn.execute("SELECT * FROM scheduled_tasks").fetchone()
    assert row["enabled"] == 0
    assert json.loads(row["preauth_json"]) == {}
    assert row["trigger_type"] == "cron" and row["cron_expr"] == "0 9 * * *"
    assert "disabled" in msg.lower()
    assert "tasks" in msg.lower()


@pytest.mark.asyncio
async def test_interval_trigger(conn):
    USER_ID_VAR.set("u1")
    await _call(name="n", prompt="p", interval_seconds=3600)
    row = conn.execute("SELECT * FROM scheduled_tasks").fetchone()
    assert row["trigger_type"] == "interval"
    assert row["interval_seconds"] == 3600
    assert row["enabled"] == 0


@pytest.mark.asyncio
async def test_no_schedule_means_webhook_only(conn):
    USER_ID_VAR.set("u1")
    await _call(name="n", prompt="p")
    row = conn.execute("SELECT * FROM scheduled_tasks").fetchone()
    assert row["trigger_type"] == "webhook_only"


@pytest.mark.asyncio
async def test_rejects_bad_input_without_creating_rows(conn):
    USER_ID_VAR.set("u1")
    assert "cron" in (await _call(name="n", prompt="p",
                                  cron_expr="not a cron")).lower()
    assert "interval" in (await _call(name="n", prompt="p",
                                      interval_seconds=5)).lower()
    assert "both" in (await _call(name="n", prompt="p", cron_expr="0 9 * * *",
                                  interval_seconds=600)).lower()
    assert await _call(name="", prompt="p")
    assert await _call(name="n", prompt="")
    assert conn.execute(
        "SELECT COUNT(*) FROM scheduled_tasks").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_rejects_without_user_identity(conn):
    USER_ID_VAR.set("")
    out = await _call(name="n", prompt="p")
    assert "identity" in out.lower()
    assert conn.execute(
        "SELECT COUNT(*) FROM scheduled_tasks").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_task_cap(conn):
    USER_ID_VAR.set("u1")
    from tasks import store as tstore
    for i in range(tasks_admin.MAX_TASKS_PER_USER):
        tstore.create_task(conn, "u1", name=f"t{i}", prompt="p",
                           trigger_type="webhook_only")
    msg = await _call(name="one more", prompt="p")
    assert "limit" in msg.lower()
    assert conn.execute(
        "SELECT COUNT(*) FROM scheduled_tasks WHERE user_id='u1'"
    ).fetchone()[0] == tasks_admin.MAX_TASKS_PER_USER


@pytest.mark.asyncio
async def test_tool_wrapper_invokes_impl(conn):
    USER_ID_VAR.set("u1")
    out = await tasks_admin.create_scheduled_task.on_invoke_tool(
        MagicMock(), '{"name": "N", "prompt": "P", "cron_expr": "0 9 * * *"}')
    assert "N" in out
    assert conn.execute(
        "SELECT COUNT(*) FROM scheduled_tasks").fetchone()[0] == 1


def test_registered_in_tasks_category():
    from skills import tool_registry
    assert "tasks" in tool_registry.CATEGORY_TOOLS
    names = [tool_registry._name(t)
             for t in tool_registry.CATEGORY_TOOLS["tasks"]]
    assert "create_scheduled_task" in names
    assert "tasks" in tool_registry.CATEGORY_DESCRIPTIONS
    assert tool_registry.category_of("create_scheduled_task") == "tasks"
