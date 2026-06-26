import asyncio
import json
import pytest

import memory_lock
import db as db_module
import memory_store as ms
from skills.memory import memory as mem_skill


def test_same_user_same_lock():
    a = memory_lock.get_user_lock("u1")
    b = memory_lock.get_user_lock("u1")
    c = memory_lock.get_user_lock("u2")
    assert a is b
    assert a is not c


@pytest.mark.asyncio
async def test_remember_serializes_under_user_lock(tmp_path):
    db_module.init_db(str(tmp_path / "m.db"))  # publishes singleton
    mem_skill.USER_ID_VAR.set("u1")
    # Hold u1's lock; remember must wait, so no row appears until released.
    lock = memory_lock.get_user_lock("u1")
    await lock.acquire()
    task = asyncio.create_task(mem_skill._remember_impl("likes tea", "preference"))
    await asyncio.sleep(0.05)
    assert ms.list_active(db_module.get_connection(), "u1") == []  # blocked
    lock.release()
    out = json.loads(await task)
    assert out["status"] == "added"
    assert len(ms.list_active(db_module.get_connection(), "u1")) == 1
