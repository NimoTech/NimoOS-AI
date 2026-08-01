import pytest

import context_compaction as cc
from db import init_db


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "m.db"))
    c.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) "
              "VALUES('s1','u1',0,0)"); c.commit()
    yield c
    c.close()


def _u(text):  # user message
    return {"role": "user", "content": text}


def _a(text):  # assistant message
    return {"role": "assistant", "content": text}


def _big_history(turns, chars):
    h = []
    for i in range(turns):
        h.append(_u(f"q{i} " + "x" * chars))
        h.append(_a(f"a{i} " + "y" * chars))
    return h


class _CallCounter:
    def __init__(self): self.n = 0

    async def __call__(self, instr, prior, fold):
        self.n += 1
        return "SHOULD_NOT_BE_USED"


async def _fake_sum(instr, prior, fold):
    return "SUMMARY:" + (prior or "")


def test_keepk_cut_user_boundary():
    h = [_u("a"), _a("b"), _u("c"), _a("d"), _u("e"), _a("f")]
    # keep last 2 turns → cut at index of 2nd-from-last user (idx 2)
    assert cc.keepk_cut(h, 2) == 2
    # fewer than K users → 0
    assert cc.keepk_cut(h, 9) == 0


@pytest.mark.asyncio
async def test_bypass_when_disabled(conn):
    conn.execute("INSERT INTO user_settings(user_id,key,value,updated_at) "
                 "VALUES('u1','compaction_enabled','0',0)"); conn.commit()
    h = _big_history(50, 500)
    counter = _CallCounter()
    block, send = await cc.compact_for_run(
        conn, session_id="s1", user_id="u1", model_name="qwen",
        history=h, current_text="hi", summarize_fn=counter)
    assert block == "" and send == h
    assert counter.n == 0, f"summarize_fn must not be called, but was called {counter.n} time(s)"


@pytest.mark.asyncio
async def test_no_trigger_under_line(conn):
    h = [_u("hello"), _a("hi")]
    counter = _CallCounter()
    block, send = await cc.compact_for_run(
        conn, session_id="s1", user_id="u1", model_name="gpt-4o",
        history=h, current_text="ok", summarize_fn=counter)
    assert send == h and block == ""        # no summary yet, nothing folded
    assert counter.n == 0, f"summarize_fn must not be called, but was called {counter.n} time(s)"


@pytest.mark.asyncio
async def test_trigger_folds_and_keeps_recent(conn):
    # small window so it triggers; many turns of CJK to exceed 70%
    conn.execute("INSERT INTO user_settings(user_id,key,value,updated_at) "
                 "VALUES('u1','context_window','2000',0)"); conn.commit()
    h = _big_history(20, 300)
    block, send = await cc.compact_for_run(
        conn, session_id="s1", user_id="u1", model_name="x",
        history=h, current_text="one more question", summarize_fn=_fake_sum)
    assert block.startswith(cc.SUMMARY_HEADER) and "SUMMARY:" in block
    # rolling_summary persisted
    row = conn.execute("SELECT rolling_summary FROM sessions WHERE id='s1'").fetchone()
    assert row["rolling_summary"] and row["rolling_summary"].startswith("SUMMARY:")
    # send is a suffix of h, starts with a user message, keeps last RECENT_TURNS turns
    assert send[0]["role"] == "user"
    assert len(send) <= cc.RECENT_TURNS * 2 + 2


@pytest.mark.asyncio
async def test_summary_failure_falls_back_to_truncation(conn):
    conn.execute("INSERT INTO user_settings(user_id,key,value,updated_at) "
                 "VALUES('u1','context_window','2000',0)"); conn.commit()
    h = _big_history(20, 300)

    async def boom(instr, prior, fold):
        raise RuntimeError("llm down")

    block, send = await cc.compact_for_run(
        conn, session_id="s1", user_id="u1", model_name="x",
        history=h, current_text="q", summarize_fn=boom)
    # no summary written, but send was hard-truncated to fit + never raised
    row = conn.execute("SELECT rolling_summary FROM sessions WHERE id='s1'").fetchone()
    assert (row["rolling_summary"] in (None, ""))
    assert len(send) < len(h) and send[0]["role"] == "user"


@pytest.mark.asyncio
async def test_terminal_truncation_keeps_last_turn(conn):
    conn.execute("INSERT INTO user_settings(user_id,key,value,updated_at) "
                 "VALUES('u1','context_window','1000',0)"); conn.commit()
    # last turn itself huge → even after folding everything else, must truncate
    h = _big_history(3, 100) + [_u("z" * 5000), _a("ok")]
    block, send = await cc.compact_for_run(
        conn, session_id="s1", user_id="u1", model_name="x",
        history=h, current_text="final turn", summarize_fn=_fake_sum)
    assert send[0]["role"] == "user" and len(send) >= 1   # never empty, never raised


@pytest.mark.asyncio
async def test_window_precheck_skips_oversized_fold(conn):
    # tiny window so fold can't fit the summarizer → summarize_fn must be skipped
    conn.execute("INSERT INTO user_settings(user_id,key,value,updated_at) "
                 "VALUES('u1','context_window','200',0)"); conn.commit()
    h = _big_history(20, 400)
    called = {"n": 0}

    async def counting(instr, prior, fold):
        called["n"] += 1
        return "S"

    block, send = await cc.compact_for_run(
        conn, session_id="s1", user_id="u1", model_name="x",
        history=h, current_text="q", summarize_fn=counting)
    assert called["n"] == 0          # fold never fits W=200 → no call
    assert len(send) < len(h)        # fell back to truncation


@pytest.mark.asyncio
async def test_bloat_gate_uses_tokens_not_chars(conn):
    # small window so it triggers; fold is ASCII (cheap per char)
    conn.execute("INSERT INTO user_settings(user_id,key,value,updated_at) "
                 "VALUES('u1','context_window','2000',0)"); conn.commit()
    h = []
    for i in range(15):
        h.append(_u("q%d " % i + "x" * 200))   # ASCII fold (~50 tok / 200 chars)
        h.append(_a("a%d " % i + "y" * 200))

    async def cjk_heavy(instr, prior, fold):
        # fewer CHARS than fold_text, but CJK → more estimated TOKENS than fold
        return "中" * (len(fold) // 2)

    block, send = await cc.compact_for_run(
        conn, session_id="s1", user_id="u1", model_name="x",
        history=h, current_text="q", summarize_fn=cjk_heavy)
    # char-len gate would ACCEPT (shorter string) and write the bloated summary;
    # token gate REJECTS it → rolling_summary stays empty, send is truncated.
    row = conn.execute("SELECT rolling_summary FROM sessions WHERE id='s1'").fetchone()
    assert row["rolling_summary"] in (None, "")
    assert len(send) < len(h)
