"""The general profile's system prompt tells the model when to ground an
answer in the user's own files instead of memory, and to reach for the
deep-search skill for broad / multi-part questions.

Evaluation on 2026-09-07 (kb-test-intel2408, DeepSeek): without such a rule
the model answered spec questions from memory (0 tool calls, 3 of 4 wrong)
whenever the question did not literally mention the knowledge base, and the
<available-skills> index alone did not change that."""
import agent as agent_module


def test_system_prompt_has_grounding_rule():
    p = agent_module.SYSTEM_PROMPT
    assert "nimoos_search" in p
    assert "rather than from memory" in p or "before answering from memory" in p
    assert "deep-search" in p
    assert "read_skill_file" in p


def test_grounding_rule_does_not_force_search_for_general_questions():
    p = agent_module.SYSTEM_PROMPT
    # The rule must carve out general knowledge / coding / writing so the
    # assistant does not start searching the NAS for every question.
    assert "General knowledge" in p or "general knowledge" in p
