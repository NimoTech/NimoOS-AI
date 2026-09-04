# tests/test_compaction_turns.py
import context_compaction as cc


def _u(t): return {"role": "user", "content": t}
def _a(t): return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": t}]}
def _r(t, id_="r"): return {"type": "reasoning", "id": id_, "summary": [{"type": "summary_text", "text": t}]}
def _fc(cid, name="web_fetch"): return {"type": "function_call", "call_id": cid, "name": name, "arguments": "{}"}
def _fo(cid, out="x"): return {"type": "function_call_output", "call_id": cid, "output": out}


def test_turn_starts_group_reasoning_with_its_call():
    items = [_u("q"), _r("t1"), _fc("c1"), _fo("c1"), _r("t2"), _fc("c2"), _fo("c2"), _a("done")]
    assert cc.turn_starts(items) == [1, 4, 7]


def test_turn_starts_parallel_calls_form_one_turn():
    items = [_u("q"), _fc("c1"), _fc("c2"), _fo("c1"), _fo("c2"), _a("done")]
    assert cc.turn_starts(items) == [1, 5]


def test_cut_keep_recent_turns():
    items = [_u("q")] + sum([[_fc(f"c{i}"), _fo(f"c{i}")] for i in range(10)], []) + [_a("done")]
    starts = cc.turn_starts(items)
    assert len(starts) == 11
    assert cc.cut_keep_recent_turns(items, 2) == starts[-2]
    assert cc.cut_keep_recent_turns(items, 50) == 0


def test_keepk_cut_falls_back_to_tool_turns_for_single_user_session():
    items = [_u("task prompt")] + sum([[_fc(f"c{i}"), _fo(f"c{i}")] for i in range(10)], [])
    cut = cc.keepk_cut(items, 6)
    assert cut == cc.cut_keep_recent_turns(items, cc.RECENT_TOOL_TURNS) > 0


def test_keepk_cut_still_uses_user_boundaries_when_enough_users():
    items = []
    for i in range(8):
        items += [_u(f"q{i}"), _a(f"a{i}")]
    assert cc.keepk_cut(items, 6) == 4   # 6th user from the end


def test_constants():
    assert cc.CLOUD_CONTEXT_WINDOW == 131072 and cc.COMPACT_LLM_TIMEOUT == 45
    assert (cc.L1_THRESHOLD, cc.L2_THRESHOLD, cc.HARD_THRESHOLD) == (0.50, 0.70, 0.85)
    assert cc.KEEP_RECENT_TOOL_RESULTS == 8 and cc.MICRO_KEEP_CHARS == 800 and cc.RECENT_TOOL_TURNS == 6


def test_prev_boundary_falls_back_to_tool_turns_for_single_user():
    items = [_u("task")] + sum([[_fc(f"c{i}"), _fo(f"c{i}")] for i in range(5)], [])
    starts = cc.turn_starts(items)          # [1, 3, 5, 7, 9]
    assert cc._prev_user_boundary(items, 7) == 5
    assert cc._prev_user_boundary(items, 1) == 0


def test_prev_boundary_keeps_user_semantics_with_many_users():
    items = []
    for i in range(4):
        items += [_u(f"q{i}"), _a(f"a{i}")]
    assert cc._prev_user_boundary(items, 6) == 4
