import json
import pytest
import agent as agent_module
import memory_store as ms
from skills.memory import memory as mem_skill


def test_compose_block_empty_when_memory_disabled(tmp_path):
    conn = agent_module.db_module.init_db(str(tmp_path / "m.db"))
    ms.add_memory(conn, "u1", "likes oat milk", "preference", priority=5)
    # enabled by default → block present
    assert "likes oat milk" in agent_module.compose_memory_block(conn, "u1")
    # disable → block empty even though the memory still exists
    conn.execute(
        "INSERT INTO user_settings(user_id,key,value,updated_at) "
        "VALUES('u1','memory_enabled','0',0)")
    conn.commit()
    assert agent_module.compose_memory_block(conn, "u1") == ""
    conn.close()


@pytest.mark.asyncio
async def test_remember_no_write_when_disabled(tmp_path):
    conn = agent_module.db_module.init_db(str(tmp_path / "m.db"))  # publishes singleton
    conn.execute(
        "INSERT INTO user_settings(user_id,key,value,updated_at) "
        "VALUES('u1','memory_enabled','0',0)")
    conn.commit()
    mem_skill.USER_ID_VAR.set("u1")
    out = json.loads(await mem_skill._remember_impl("should not persist", "fact"))
    assert out["status"] == "disabled"
    assert ms.list_active(conn, "u1") == []
    conn.close()
