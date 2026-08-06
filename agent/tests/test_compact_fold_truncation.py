import pytest
import context_compaction as cc
from db import init_db


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "m.db"))
    c.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) "
              "VALUES('s1','u1',0,0)"); c.commit()
    # small summarizer window so a FULL tool output would never fit
    c.execute("INSERT INTO user_settings(user_id,key,value,updated_at) "
              "VALUES('u1','context_window','3000',0)"); c.commit()
    yield c
    c.close()


def _u(t): return {"role": "user", "content": t}
def _fco(out): return {"type": "function_call_output", "output": out}


@pytest.mark.asyncio
async def test_huge_tool_output_still_summarizes_with_truncated_fold(conn):
    # history: 8 user turns each followed by a giant tool output (~6000 chars).
    # FULL fold would be ~50k chars >> window → old code would skip/fail.
    h = []
    for i in range(8):
        h.append(_u("question %d" % i))
        h.append(_fco("D" * 6000))
    seen = {}
    async def fake(instr, prior, fold):
        seen["fold"] = fold
        return "summary"
    block, send = await cc.compact_for_run(
        conn, session_id="s1", user_id="u1", model_name="x",
        history=h, current_text="final turn", summarize_fn=fake)
    # summarize WAS called (not skipped) — compaction works for tool-heavy session
    assert "fold" in seen
    # the giant outputs in the fold are TRUNCATED (cap 500 + marker), not full 6000
    assert "…[+" in seen["fold"]
    assert seen["fold"].count("D" * 6000) == 0
    # rolling_summary written
    row = conn.execute("SELECT rolling_summary FROM sessions WHERE id='s1'").fetchone()
    assert row["rolling_summary"] == "summary"
