import json
import pytest

import context_compaction as cc
from db import init_db


class _Tool:
    def __init__(self, name, desc, schema):
        self.name = name
        self.description = desc
        self.params_json_schema = schema


def test_estimate_tools_tokens_sums_and_empty():
    assert cc.estimate_tools_tokens([]) == 0
    assert cc.estimate_tools_tokens(None) == 0
    one = cc.estimate_tools_tokens([_Tool("a", "does a", {"type": "object"})])
    two = cc.estimate_tools_tokens([_Tool("a", "does a", {"type": "object"}),
                                    _Tool("b", "does b thing", {"type": "object",
                                          "properties": {"x": {"type": "string"}}})])
    assert one > 0 and two > one
    # non-empty includes the fixed framework boilerplate base
    assert one >= cc.TOOLS_BASE_OVERHEAD
    # base only when non-empty
    assert cc.estimate_tools_tokens([]) == 0


def test_estimate_tools_tokens_tolerates_bad_tool():
    class Bad:  # missing all attrs
        pass
    # must not raise; counts what it can (name/desc/params default to empty)
    assert cc.estimate_tools_tokens([Bad()]) >= 0


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "m.db"))
    c.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) "
              "VALUES('s1','u1',0,0)"); c.commit()
    yield c
    c.close()


def _u(t): return {"role": "user", "content": t}
def _a(t): return {"role": "assistant", "content": t}


@pytest.mark.asyncio
async def test_overhead_makes_compaction_trigger_earlier(conn):
    conn.execute("INSERT INTO user_settings(user_id,key,value,updated_at) "
                 "VALUES('u1','context_window','2000',0)"); conn.commit()
    # history with enough turns so keepk_cut can produce cut>0 (RECENT_TURNS=6,
    # need >6 user messages so older ones can be folded)
    h = []
    for i in range(8):
        h.append(_u("问题%d " % i + "中" * 30))
        h.append(_a("回答%d " % i + "答" * 30))
    calls = {"n": 0}
    async def fake(instr, prior, fold):
        calls["n"] += 1
        return "S"
    # no overhead → may not trigger (history small relative to window)
    await cc.compact_for_run(conn, session_id="s1", user_id="u1", model_name="x",
                             history=h, current_text="q", summarize_fn=fake,
                             overhead_tokens=0)
    base_calls = calls["n"]
    # big overhead → must push over the line → trigger
    calls["n"] = 0
    await cc.compact_for_run(conn, session_id="s1", user_id="u1", model_name="x",
                             history=h, current_text="q", summarize_fn=fake,
                             overhead_tokens=5000)
    assert calls["n"] >= 1 and calls["n"] >= base_calls


@pytest.mark.asyncio
async def test_terminal_truncate_accounts_for_overhead(conn):
    conn.execute("INSERT INTO user_settings(user_id,key,value,updated_at) "
                 "VALUES('u1','context_window','3000',0)"); conn.commit()
    h = []
    for i in range(10):
        h.append(_u("q%d " % i + "中" * 100)); h.append(_a("a%d " % i + "答" * 100))
    async def fake(instr, prior, fold): return "摘要"
    # huge overhead eats most of the window → sent history must shrink hard
    block, send = await cc.compact_for_run(
        conn, session_id="s1", user_id="u1", model_name="x", history=h,
        current_text="末", summarize_fn=fake, overhead_tokens=1500)
    line = int(cc.THRESHOLD * 3000)
    assert (1500 + cc.estimate_tokens("摘要") + cc.estimate_tokens("末")
            + cc.estimate_messages_tokens(send)) <= line
    assert send and send[0]["role"] == "user"


def test_compute_usage_counts_overhead(conn):
    conn.execute("INSERT INTO messages(id,session_id,role,content,created_at) "
                 "VALUES('m','s1','history',?,1)",
                 (json.dumps([_u("你好" * 20)]),)); conn.commit()
    without = cc.compute_usage(conn, session_id="s1", user_id="u1", model="qwen")
    conn.execute("UPDATE sessions SET last_overhead_tokens=777 WHERE id='s1'"); conn.commit()
    withov = cc.compute_usage(conn, session_id="s1", user_id="u1", model="qwen")
    assert withov["tokens"] == without["tokens"] + 777
