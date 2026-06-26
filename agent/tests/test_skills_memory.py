import json
import pytest

import db as db_module
import memory_store as ms
from skills import ALL_TOOLS
from skills.memory import memory as mem_skill


@pytest.fixture
def conn(tmp_path):
    c = db_module.init_db(str(tmp_path / "m.db"))  # also publishes the singleton
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _reset_vars():
    mem_skill.USER_ID_VAR.set("")
    mem_skill.SESSION_ID_VAR.set("")
    yield


def test_memory_tools_in_all_tools():
    names = {t.name for t in ALL_TOOLS}
    assert "remember" in names
    assert "forget" in names


@pytest.mark.asyncio
async def test_remember_writes_scoped_to_user(conn):
    mem_skill.USER_ID_VAR.set("u1")
    mem_skill.SESSION_ID_VAR.set("s1")
    out = json.loads(await mem_skill._remember_impl("likes oat milk", "preference", 4))
    assert out["status"] == "added"
    rows = ms.list_active(conn, "u1", now=None)
    assert len(rows) == 1
    assert rows[0]["source"] == "tool"
    assert rows[0]["origin_session_id"] == "s1"
    assert rows[0]["priority"] == 4


@pytest.mark.asyncio
async def test_remember_dedups(conn):
    mem_skill.USER_ID_VAR.set("u1")
    await mem_skill._remember_impl("likes oat milk", "preference")
    out = json.loads(await mem_skill._remember_impl("Likes  Oat Milk", "preference"))
    assert out["status"] == "duplicate"
    assert len(ms.list_active(conn, "u1")) == 1


@pytest.mark.asyncio
async def test_remember_rejects_bad_kind(conn):
    mem_skill.USER_ID_VAR.set("u1")
    out = json.loads(await mem_skill._remember_impl("x", "nonsense"))
    assert "error" in out


@pytest.mark.asyncio
async def test_remember_needs_user_context(conn):
    out = json.loads(await mem_skill._remember_impl("x", "fact"))
    assert "error" in out
    assert len(ms.list_active(conn, "u1")) == 0


@pytest.mark.asyncio
async def test_forget_by_text_disables(conn):
    mem_skill.USER_ID_VAR.set("u1")
    await mem_skill._remember_impl("lives in Berlin", "fact")
    out = json.loads(await mem_skill._forget_impl("berlin"))
    assert out["status"] == "forgotten"
    assert len(out["ids"]) == 1
    assert len(ms.list_active(conn, "u1")) == 0


@pytest.mark.asyncio
async def test_forget_by_id(conn):
    mem_skill.USER_ID_VAR.set("u1")
    mid = ms.add_memory(conn, "u1", "to remove", "fact", source="tool")
    out = json.loads(await mem_skill._forget_impl(mid))
    assert out["ids"] == [mid]
    assert len(ms.list_active(conn, "u1")) == 0
