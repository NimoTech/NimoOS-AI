import pytest
from agents.run import CallModelData, ModelInputData

import compaction_filter as cf
import context_compaction as cc
import run_context as rc
import tool_output as to


def _fc(cid): return {"type": "function_call", "call_id": cid, "name": "web_fetch", "arguments": "{}"}
def _fo(cid, out): return {"type": "function_call_output", "call_id": cid, "output": out}
def _u(t): return {"role": "user", "content": t}


def _items(n, size):
    return [_u("go")] + sum([[_fc(f"c{i}"), _fo(f"c{i}", "x" * size)] for i in range(n)], [])


def _data(items, instructions="SYS"):
    return CallModelData(model_data=ModelInputData(input=items, instructions=instructions), agent=None, context=None)


def _ctx(window, **kw):
    ctx = rc.RunCtx(session_id="s", user_id="u", model_name="m", provider_type="other", window=window, **kw)
    rc.RUN_CTX_VAR.set(ctx)
    return ctx


@pytest.mark.asyncio
async def test_no_ctx_passthrough():
    to.OFFLOAD_DIR_VAR.set("")
    rc.RUN_CTX_VAR.set(None)
    d = _data(_items(3, 10))
    out = await cf.compaction_filter(d)
    assert out is d.model_data


@pytest.mark.asyncio
async def test_compaction_disabled_passthrough():
    to.OFFLOAD_DIR_VAR.set("")
    _ctx(window=100, compaction_enabled=False)
    d = _data(_items(3, 10))
    out = await cf.compaction_filter(d)
    assert out is d.model_data


@pytest.mark.asyncio
async def test_under_budget_untouched(tmp_path):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    ctx = _ctx(window=1_000_000)
    items = _items(20, 2000)
    out = await cf.compaction_filter(_data(items))
    assert out.input == items and ctx.l1_count == 0 and out.instructions == "SYS"


@pytest.mark.asyncio
async def test_l1_triggers_on_provider_usage(tmp_path):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    ctx = _ctx(window=10_000, last_input_tokens=6_000, items_seen_at_last_call=41)
    items = _items(20, 2000)
    out = await cf.compaction_filter(_data(items))
    assert ctx.l1_count == 16
    outs = [m for m in out.input if m.get("type") == "function_call_output"]
    assert all("compacted" in m["output"] for m in outs[:16])
    assert items[2]["output"] == "x" * 2000            # originals untouched
    assert ctx.extra["last_sent_len"] == len(items)


def test_budget_uses_provider_path_when_last_input_tokens_set():
    ctx = rc.RunCtx(session_id="s", user_id="u", model_name="m", provider_type="other",
                     window=1_000, last_input_tokens=500, items_seen_at_last_call=1)
    items = [_u("a"), _u("b" * 4000)]
    assert cf.budget(ctx, items) == 500 + cf.estimate_since(items, 1)


def test_budget_uses_estimate_path_when_no_provider_count():
    ctx = rc.RunCtx(session_id="s", user_id="u", model_name="m", provider_type="other", window=1_000)
    items = [_u("hello")]
    assert cf.budget(ctx, items) == (ctx.overhead_tokens + cc.estimate_tokens(ctx.summary)
                                      + cc.estimate_messages_tokens(items))


@pytest.mark.asyncio
async def test_l1_fires_despite_stale_provider_budget(tmp_path):
    # Provider reports a small last-call count (as if the previous outgoing
    # list had already been L1-compacted), but `items` here are the
    # ORIGINALS again (persisted history, never mutated) — budget() alone
    # (3000 + estimate_since(items, 39) ~= 3000 + 575 = 3575) stays under
    # L1_THRESHOLD*window (5000) and would wrongly skip L1. The real
    # (uncompacted) size is ~11582 tokens (see test_l1_triggers_on_provider_
    # usage), which _estimate() sees regardless of the provider number — with
    # est = max(budget, _estimate), L1 must still fire.
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    items = _items(20, 2000)
    ctx = _ctx(window=10_000, last_input_tokens=3_000, items_seen_at_last_call=len(items) - 2)
    out = await cf.compaction_filter(_data(items))
    assert ctx.l1_count > 0
    assert items[2]["output"] == "x" * 2000            # originals untouched
    assert out.input is not items


@pytest.mark.asyncio
async def test_l2_folds_and_updates_ctx(tmp_path):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    calls = []
    async def summ(instr, prior, fold):
        calls.append((prior, len(fold))); return "S:" + str(len(calls))
    # window=4400: L1 alone (est ~6124 after compacting old outputs) still
    # exceeds L2_THRESHOLD*W (~3080), so L2 must run; after folding, the
    # remaining last-6-turns estimate (~3474) stays under HARD_THRESHOLD*W
    # (~3740) so hard truncation does not also fire — window=4000 from the
    # brief left only ~-74 tokens of margin there, tripping hard truncation
    # as a side effect and invalidating this assertion.
    ctx = _ctx(window=4_400, summarize_fn=summ)
    items = _items(20, 2000)
    out = await cf.compaction_filter(_data(items))
    assert ctx.l2_count == 1 and ctx.summary == "S:1" and ctx.fold_idx > 0
    assert ctx.trunc_count == 0
    assert len(out.input) == len(items) - ctx.fold_idx
    assert cc.SUMMARY_HEADER in out.instructions and out.instructions.startswith("SYS")
    assert items[2]["output"] == "x" * 2000            # originals untouched
    # second call: prefix already folded, summary appended once, not twice
    out2 = await cf.compaction_filter(_data(items + [_fc("z"), _fo("z", "y")]))
    assert out2.instructions.count(cc.SUMMARY_HEADER) == 1


@pytest.mark.asyncio
async def test_l2_bloat_gate_rejects_longer_summary(tmp_path):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    async def bloat(instr, prior, fold): return "中" * (len(fold) * 2)
    ctx = _ctx(window=4_000, summarize_fn=bloat)
    out = await cf.compaction_filter(_data(_items(20, 2000)))
    assert ctx.l2_count == 0 and ctx.summary == "" and ctx.fold_idx == 0
    assert ctx.trunc_count == 1                           # hard truncation took over


@pytest.mark.asyncio
async def test_hard_truncation_keeps_head_user_after_fold(tmp_path):
    # window=3_000 with 20 x 2000-char outputs: L1 fires (keeps the last 4
    # outputs whole), L2 folds (accepted since "S" is trivially shorter than
    # fold_text), but the remaining last-6-turns estimate still exceeds
    # HARD_THRESHOLD*3000 (2550) so hard truncation also fires afterward.
    # (Was window=4_000 with KEEP_RECENT_TOOL_RESULTS=8; with 4 kept the
    # post-L1 estimate no longer crosses 0.85*4000.)
    # After the L2 fold, items[0] is no longer the original user message —
    # truncate_turns' own "keep items[0] if it's a user message" fallback
    # can't see it, so compaction_filter must re-attach it from `full`.
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    async def summ(instr, prior, fold): return "S"
    ctx = _ctx(window=3_000, summarize_fn=summ)
    items = _items(20, 2000)
    out = await cf.compaction_filter(_data(items))
    assert ctx.l2_count == 1 and ctx.trunc_count == 1
    assert out.input[0] is items[0]                    # the original user prompt


def test_with_summary_strips_preexisting_summary_block():
    ctx = rc.RunCtx(session_id="s", user_id="u", model_name="m", provider_type="other",
                     window=100, summary="NEW")
    instructions = "SYS\n\n" + cc.summary_block("OLD")
    out = cf._with_summary(ctx, instructions)
    assert out.count(cc.SUMMARY_HEADER) == 1
    assert "NEW" in out and "OLD" not in out


@pytest.mark.asyncio
async def test_filter_never_raises(monkeypatch):
    to.OFFLOAD_DIR_VAR.set("")
    _ctx(window=10)
    monkeypatch.setattr(cf, "micro_compact", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    d = _data(_items(5, 5000))
    out = await cf.compaction_filter(d)
    assert out is d.model_data


@pytest.mark.asyncio
async def test_hooks_record_usage_and_write_session(tmp_path):
    to.OFFLOAD_DIR_VAR.set("")
    from db import init_db
    conn = init_db(str(tmp_path / "h.db"))
    conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) VALUES('s','u',0,0)"); conn.commit()
    ctx = _ctx(window=100, conn=conn)
    ctx.extra["last_sent_len"] = 7
    class U: input_tokens = 4321
    class R: usage = U()
    await cf.ContextHooks().on_llm_end(None, None, R())
    assert ctx.last_input_tokens == 4321 and ctx.items_seen_at_last_call == 7
    assert conn.execute("SELECT last_real_input_tokens FROM sessions WHERE id='s'").fetchone()[0] == 4321
    assert ctx.peak_input_tokens == 4321


@pytest.mark.asyncio
async def test_hooks_track_peak_input_tokens_across_calls():
    to.OFFLOAD_DIR_VAR.set("")
    ctx = _ctx(window=100)
    class U1: input_tokens = 4321
    class R1: usage = U1()
    class U2: input_tokens = 1000
    class R2: usage = U2()
    await cf.ContextHooks().on_llm_end(None, None, R1())
    await cf.ContextHooks().on_llm_end(None, None, R2())
    assert ctx.last_input_tokens == 1000
    assert ctx.peak_input_tokens == 4321


@pytest.mark.asyncio
async def test_hooks_count_llm_calls():
    to.OFFLOAD_DIR_VAR.set("")
    ctx = _ctx(window=100)
    class U: input_tokens = 10
    class R: usage = U()
    await cf.ContextHooks().on_llm_end(None, None, R())
    await cf.ContextHooks().on_llm_end(None, None, R())
    assert ctx.extra["llm_calls"] == 2


@pytest.mark.asyncio
async def test_hooks_tolerate_missing_usage():
    to.OFFLOAD_DIR_VAR.set("")
    ctx = _ctx(window=100)
    class R: usage = None
    await cf.ContextHooks().on_llm_end(None, None, R())
    assert ctx.last_input_tokens == 0


@pytest.mark.asyncio
async def test_l2_disabled_after_two_failures_and_summarizer_not_called_again(tmp_path):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    calls = []
    async def always_empty(instr, prior, fold):
        calls.append(1)
        return ""
    ctx = _ctx(window=4_000, summarize_fn=always_empty)
    items = _items(20, 2000)
    await cf.compaction_filter(_data(items))
    assert ctx.l2_fail_count == 1 and ctx.l2_disabled is False
    await cf.compaction_filter(_data(items))
    assert ctx.l2_disabled is True and len(calls) == 2
    assert ctx.trunc_count >= 1
    trunc_before = ctx.trunc_count
    await cf.compaction_filter(_data(items))
    assert len(calls) == 2                      # summarizer not invoked a 3rd time
    assert ctx.trunc_count > trunc_before        # hard truncation still runs


@pytest.mark.asyncio
async def test_l1_pass_counts_reasoning_stubs(tmp_path):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    ctx = _ctx(window=10_000, last_input_tokens=6_000, items_seen_at_last_call=41)

    def _r(t, id_): return {"type": "reasoning", "id": id_, "summary": [{"type": "summary_text", "text": t}]}
    items = [_u("go")]
    for i in range(20):
        items += [_r(f"think {i}", f"r{i}"), _fc(f"c{i}"), _fo(f"c{i}", "x" * 2000)]
    await cf.compaction_filter(_data(items))
    assert ctx.l1_reasoning_count > 0


@pytest.mark.asyncio
async def test_convergence_over_many_turns(tmp_path):
    """F8: 40 growing turns must never blow the budget once compaction has
    had a chance to kick in, tool-call/output pairs must stay intact in the
    outgoing copy, and the persisted originals must never be mutated."""
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    calls = []
    async def summ(instr, prior, fold):
        calls.append(1)
        return f"SUMMARY {len(calls)}"
    W = 32_000
    ctx = _ctx(window=W, summarize_fn=summ)

    def _r(id_):
        return {"type": "reasoning", "id": id_, "summary": [{"type": "summary_text", "text": "t" * 4000}]}

    items = [_u("go")]
    for turn in range(40):
        cid = f"c{turn}"
        items = items + [_r(f"r{turn}"), _fc(cid), _fo(cid, "o" * 4000)]
        out = await cf.compaction_filter(_data(list(items)))
        if turn >= 5:
            assert cc.estimate_messages_tokens(out.input) <= 0.85 * W + 2000
        fcs = {m["call_id"] for m in out.input if m.get("type") == "function_call"}
        fos = {m["call_id"] for m in out.input if m.get("type") == "function_call_output"}
        assert fcs == fos

    # originals untouched throughout
    assert items[3]["output"] == "o" * 4000
