import json
import pytest

import db as db_module
import agent as agent_module
from skills import ALL_TOOLS
from skills.memory import memory as mem_skill
from skills import tool_registry as tr
from tests.conftest import unfence


@pytest.fixture(autouse=True)
def _reset(tmp_path):
    db_module.init_db(str(tmp_path / "m.db"))
    mem_skill.USER_ID_VAR.set("")
    yield


def test_recall_in_all_tools_and_core():
    assert "recall" in {t.name for t in ALL_TOOLS}
    assert "recall" in tr.CORE_TOOL_NAMES


def test_system_prompt_mentions_recall():
    assert "recall" in agent_module.SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_recall_passes_user_id_and_returns_hits(monkeypatch):
    mem_skill.USER_ID_VAR.set("u1")
    seen = {}
    async def fake_query(user_id, query, top_k=5):
        seen["user_id"] = user_id; seen["query"] = query
        return {"hits": [{"text": "we discussed docker", "session_id": "s9",
                          "chunk_no": 0, "created_at": 1, "score": 0.8}]}
    monkeypatch.setattr(mem_skill, "_query_agent_memory", fake_query)
    # Recalled memories can contain external content — fenced as untrusted (L3).
    out = json.loads(unfence(await mem_skill._recall_impl("docker error"),
                             source="recall"))
    assert seen["user_id"] == "u1" and seen["query"] == "docker error"
    assert out["hits"][0]["text"] == "we discussed docker"


@pytest.mark.asyncio
async def test_recall_soft_fails_when_parser_down(monkeypatch):
    mem_skill.USER_ID_VAR.set("u1")
    async def boom(user_id, query, top_k=5): raise RuntimeError("down")
    monkeypatch.setattr(mem_skill, "_query_agent_memory", boom)
    out = json.loads(await mem_skill._recall_impl("x"))
    assert out["status"] == "unavailable"


@pytest.mark.asyncio
async def test_recall_disabled_when_memory_off(monkeypatch):
    mem_skill.USER_ID_VAR.set("u1")
    db_module.get_connection().execute(
        "INSERT INTO user_settings(user_id,key,value,updated_at) "
        "VALUES('u1','memory_enabled','0',0)")
    db_module.get_connection().commit()
    out = json.loads(await mem_skill._recall_impl("x"))
    assert out["status"] == "disabled"
