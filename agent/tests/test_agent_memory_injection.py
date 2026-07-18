import agent as agent_module
import memory_store as ms
from skills.memory import memory as mem_skill


def test_agent_references_same_memory_objects():
    # agent.py must use the same ContextVar/module objects the skill exposes,
    # else per-run identity set in agent.py won't be seen by the tool.
    assert agent_module.memory_skills.USER_ID_VAR is mem_skill.USER_ID_VAR
    assert agent_module.memory_skills.SESSION_ID_VAR is mem_skill.SESSION_ID_VAR
    assert agent_module.memory_store is ms


def test_injection_appends_block_for_compose_profile(tmp_path):
    conn = agent_module.db_module.init_db(str(tmp_path / "m.db"))
    ms.add_memory(conn, "u1", "likes oat milk", "preference", priority=5)
    base = "BASE PROMPT"
    block = agent_module.compose_memory_block(conn, "u1")
    assert block != ""
    assert "## About this user" in block
    assert "likes oat milk" in block
    conn.close()


def test_injection_empty_when_no_memories(tmp_path):
    conn = agent_module.db_module.init_db(str(tmp_path / "m.db"))
    assert agent_module.compose_memory_block(conn, "u1") == ""
    conn.close()
