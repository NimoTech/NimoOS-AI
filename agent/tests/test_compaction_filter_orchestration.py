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
    rc.RUN_CTX_VAR.set(None)
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
    assert ctx.l1_count == 12
    outs = [m for m in out.input if m.get("type") == "function_call_output"]
    assert all("compacted" in m["output"] for m in outs[:12])
    assert items[2]["output"] == "x" * 2000            # originals untouched
    assert ctx.extra["last_sent_len"] == len(items)


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
async def test_filter_never_raises(monkeypatch):
    _ctx(window=10)
    monkeypatch.setattr(cf, "micro_compact", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    d = _data(_items(5, 5000))
    out = await cf.compaction_filter(d)
    assert out is d.model_data


@pytest.mark.asyncio
async def test_hooks_record_usage_and_write_session(tmp_path):
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


@pytest.mark.asyncio
async def test_hooks_tolerate_missing_usage():
    ctx = _ctx(window=100)
    class R: usage = None
    await cf.ContextHooks().on_llm_end(None, None, R())
    assert ctx.last_input_tokens == 0
