import pytest

import context_compaction as cc
from db import init_db


def _conn(tmp_path, name="m.db"):
    conn = init_db(str(tmp_path / name))
    conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) "
                 "VALUES('s1','u1',0,0)")
    conn.execute("INSERT INTO user_settings(user_id,key,value,updated_at) "
                 "VALUES('u1','memory_enabled','1',0)")
    # tiny window → compaction triggers deterministically
    conn.execute("INSERT INTO user_settings(user_id,key,value,updated_at) "
                 "VALUES('u1','context_window','100',0)")
    conn.commit()
    return conn


def _history(n_turns):
    h = []
    for i in range(n_turns):
        h.append({"role": "user", "content": f"question {i} " + "x" * 40})
        h.append({"role": "assistant", "content": f"answer {i} " + "y" * 40})
    return h


async def _ok_summarize(instr, prior, fold):
    return "SUM"


@pytest.mark.asyncio
async def test_fold_advances_cursor_and_folds_only_delta(tmp_path):
    conn = _conn(tmp_path)
    # Base fixture's context_window=100 is smaller than the fixed Chinese
    # SUMMARIZE_INSTRUCTION overhead (~103 tokens) alone, so no fold could
    # ever fit the summarizer window — bump just enough (still small vs the
    # ~273-token full history) to keep the over-line trigger deterministic
    # while letting the actual fold succeed.
    conn.execute("UPDATE user_settings SET value='200' "
                 "WHERE user_id='u1' AND key='context_window'"); conn.commit()
    hist = _history(8)   # 8 user turns > RECENT_TURNS=6 → cut > 0
    seen = {}

    async def spy_summarize(instr, prior, fold):
        seen["fold"] = fold
        return "SUM"

    block, send = await cc.compact_for_run(
        conn, session_id="s1", user_id="u1", model_name="m",
        history=hist, current_text="q", summarize_fn=spy_summarize)
    F = conn.execute("SELECT folded_upto FROM sessions WHERE id='s1'"
                     ).fetchone()["folded_upto"]
    assert F > 0
    assert send == hist[F:] or send == hist[F:][len(hist[F:]) - len(send):]
    # second run: fold must start AT the cursor, not from 0
    hist2 = hist + _history(1)
    await cc.compact_for_run(
        conn, session_id="s1", user_id="u1", model_name="m",
        history=hist2, current_text="q", summarize_fn=spy_summarize)
    assert "question 0" not in seen["fold"]   # already-folded turns not re-fed


@pytest.mark.asyncio
async def test_estimate_excludes_folded_prefix(tmp_path):
    # With folded_upto covering all but the tail, a session whose UNFOLDED
    # remainder fits the line must NOT trigger fold/truncation again.
    conn = _conn(tmp_path)
    conn.execute("UPDATE user_settings SET value='2000' "
                 "WHERE user_id='u1' AND key='context_window'")
    hist = _history(8)
    conn.execute("UPDATE sessions SET rolling_summary='SUM', folded_upto=? "
                 "WHERE id='s1'", (len(hist) - 2,))
    conn.commit()
    called = {}

    async def spy(instr, prior, fold):
        called["yes"] = True
        return "SUM2"

    block, send = await cc.compact_for_run(
        conn, session_id="s1", user_id="u1", model_name="m",
        history=hist, current_text="q", summarize_fn=spy)
    assert "yes" not in called          # no re-fold
    assert send == hist[len(hist) - 2:]  # sends only the unfolded tail


@pytest.mark.asyncio
async def test_summarize_failure_sends_unfolded_remainder(tmp_path):
    conn = _conn(tmp_path)
    hist = _history(8)
    conn.execute("UPDATE sessions SET rolling_summary='OLD', folded_upto=4 "
                 "WHERE id='s1'"); conn.commit()

    async def boom(instr, prior, fold):
        raise RuntimeError("llm down")

    block, send = await cc.compact_for_run(
        conn, session_id="s1", user_id="u1", model_name="m",
        history=hist, current_text="q", summarize_fn=boom)
    # cursor unchanged, candidate before terminal truncate is hist[4:] —
    # send must be a tail slice of hist[4:], never include folded items
    F = conn.execute("SELECT folded_upto FROM sessions WHERE id='s1'"
                     ).fetchone()["folded_upto"]
    assert F == 4
    assert all(m in hist[4:] for m in send)


@pytest.mark.asyncio
async def test_stale_cursor_resets(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("UPDATE sessions SET folded_upto=999 WHERE id='s1'")
    conn.commit()
    hist = _history(2)
    block, send = await cc.compact_for_run(
        conn, session_id="s1", user_id="u1", model_name="m",
        history=hist, current_text="q", summarize_fn=_ok_summarize)
    assert len(send) >= 1               # never empties on stale cursor


@pytest.mark.asyncio
async def test_drop_enqueues_immediate_recall_job(tmp_path):
    conn = _conn(tmp_path)
    hist = _history(8)
    await cc.compact_for_run(
        conn, session_id="s1", user_id="u1", model_name="m",
        history=hist, current_text="q", summarize_fn=_ok_summarize, now=5000)
    row = conn.execute("SELECT enqueued_at FROM recall_index_jobs "
                       "WHERE session_id='s1'").fetchone()
    assert row is not None
    assert row["enqueued_at"] <= 5000 - 120   # backdated → claimable now


@pytest.mark.asyncio
async def test_no_drop_no_enqueue(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("UPDATE user_settings SET value='100000' "
                 "WHERE user_id='u1' AND key='context_window'"); conn.commit()
    await cc.compact_for_run(
        conn, session_id="s1", user_id="u1", model_name="m",
        history=_history(2), current_text="q", summarize_fn=_ok_summarize)
    assert conn.execute("SELECT 1 FROM recall_index_jobs "
                        "WHERE session_id='s1'").fetchone() is None


def test_summary_block_hint_flag():
    assert cc.summary_block("S", recall_hint=True).endswith(cc.RECALL_HINT)
    assert cc.RECALL_HINT not in cc.summary_block("S")
    assert cc.summary_block("", recall_hint=True) == ""


@pytest.mark.asyncio
async def test_compact_appends_hint_iff_memory_enabled(tmp_path):
    conn = _conn(tmp_path)   # memory_enabled=1
    # Base fixture's context_window=100 is smaller than the fixed Chinese
    # SUMMARIZE_INSTRUCTION overhead (~103 tokens) alone, so no fold could
    # ever succeed and the summary would stay permanently empty (hint only
    # appears on a non-empty summary) — bump so the fold actually produces a
    # summary, same rationale as test_fold_advances_cursor_and_folds_only_delta.
    conn.execute("UPDATE user_settings SET value='200' "
                 "WHERE user_id='u1' AND key='context_window'"); conn.commit()
    block, _ = await cc.compact_for_run(
        conn, session_id="s1", user_id="u1", model_name="m",
        history=_history(8), current_text="q", summarize_fn=_ok_summarize)
    assert cc.RECALL_HINT in block

    conn2 = _conn(tmp_path, name="m2.db")
    conn2.execute("UPDATE user_settings SET value='200' "
                 "WHERE user_id='u1' AND key='context_window'")
    conn2.execute("UPDATE user_settings SET value='0' "
                  "WHERE user_id='u1' AND key='memory_enabled'"); conn2.commit()
    block2, _ = await cc.compact_for_run(
        conn2, session_id="s1", user_id="u1", model_name="m",
        history=_history(8), current_text="q", summarize_fn=_ok_summarize)
    assert cc.RECALL_HINT not in block2
