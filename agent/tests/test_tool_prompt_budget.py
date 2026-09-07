import json
import agent as agent_mod


def test_turn1_general_tool_schema_under_budget():
    """The serialized tool-schema size for turn 1 of a general profile must stay
    small (prevents someone from bloating turn-1 prompt size by carelessly
    adding tools to the always-on set).

    Only counts tools the model actually sees on turn 1 (always-on + expand_tools);
    gated tools are in the list but have is_enabled False, so they don't enter the
    prompt. Threshold is a rough char-count estimate (~4 char/token).
    """
    from skills import tool_gating as tg
    tg.UNLOCKED_VAR.set(set())          # empty unlocked set = turn 1
    tools = agent_mod.select_tools_for_run([], session_id="s1", profile=None)
    visible = [t for t in tools
               if getattr(t, "is_enabled", True) is True
               or (callable(getattr(t, "is_enabled", True))
                   and t.is_enabled(None, None))]
    size = 0
    for t in visible:
        size += len(getattr(t, "name", ""))
        size += len(getattr(t, "description", "") or "")
        schema = getattr(t, "params_json_schema", None)
        if schema:
            size += len(json.dumps(schema, ensure_ascii=False))
    # 2026-09-07 (P4, spec §7): the always-on set is now 12 tools + expand_tools
    # (update_plan / delegate joined deliberately; their descriptions are kept
    # terse). Threshold raised 8000 -> 9500 (~2.4k tokens) — still a bloat guard.
    assert size < 9500, f"turn-1 tool schema size too large: {size} chars"
