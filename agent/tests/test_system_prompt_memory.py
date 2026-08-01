import agent as agent_module
from skills import tool_registry as tr


def test_remember_forget_are_core():
    assert "remember" in tr.CORE_TOOL_NAMES
    assert "forget" in tr.CORE_TOOL_NAMES


def test_no_memory_category():
    assert "memory" not in tr.CATEGORY_TOOLS
    assert "memory" not in tr.CATEGORY_DESCRIPTIONS


def test_system_prompt_mentions_memory():
    p = agent_module.SYSTEM_PROMPT
    assert "remember" in p
    assert "memory" in p.lower()
