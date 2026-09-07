import pytest

import context_compaction as cc
import compaction_filter as cf
import run_context as rc
from db import init_db


def test_runctx_has_plan_fields_with_defaults():
    ctx = rc.RunCtx(session_id="s", user_id="u", model_name="m", provider_type="other", window=1000)
    assert ctx.plan == [] and ctx.depth == 0 and ctx.sink is None and ctx.parent_call_id == ""


def test_validate_plan_normalises_and_limits():
    steps = cc.validate_plan([{"id": "a", "title": "Read seen.json", "status": "done"},
                              {"id": "b", "title": "Collect A", "status": "in_progress", "note": "3 feeds"}])
    assert steps == [{"id": "a", "title": "Read seen.json", "status": "done", "note": ""},
                     {"id": "b", "title": "Collect A", "status": "in_progress", "note": "3 feeds"}]
    with pytest.raises(ValueError, match="status"):
        cc.validate_plan([{"id": "a", "title": "x", "status": "doing"}])
    with pytest.raises(ValueError, match="30"):
        cc.validate_plan([{"id": str(i), "title": "x", "status": "pending"} for i in range(31)])
    with pytest.raises(ValueError, match="200"):
        cc.validate_plan([{"id": "a", "title": "x" * 201, "status": "pending"}])
    with pytest.raises(ValueError, match="id"):
        cc.validate_plan([{"title": "x", "status": "pending"}])
    with pytest.raises(ValueError, match="list"):
        cc.validate_plan({"id": "a"})


def test_plan_block_renders_checklist_and_is_empty_for_no_steps():
    assert cc.plan_block([]) == ""
    out = cc.plan_block([{"id": "a", "title": "Read", "status": "done", "note": ""},
                         {"id": "b", "title": "Collect", "status": "in_progress", "note": "A-F"},
                         {"id": "c", "title": "Write", "status": "pending", "note": ""}])
    assert out.startswith(cc.PLAN_HEADER)
    assert "[x] a. Read" in out and "[>] b. Collect — A-F" in out and "[ ] c. Write" in out
    assert out.rstrip().endswith("</plan>")


def test_with_summary_appends_plan_after_summary_idempotently():
    ctx = rc.RunCtx(session_id="s", user_id="u", model_name="m", provider_type="other",
                    window=1000, summary="SUM", plan=[{"id": "a", "title": "T", "status": "pending", "note": ""}])
    ctx.extra["base_instructions"] = "SYS"
    once = cf._with_summary(ctx, "SYS")
    twice = cf._with_summary(ctx, once)
    assert once == twice
    assert once.index(cc.SUMMARY_HEADER) < once.index(cc.PLAN_HEADER)
    assert once.count(cc.PLAN_HEADER) == 1


def test_with_summary_plan_only_no_summary():
    ctx = rc.RunCtx(session_id="s", user_id="u", model_name="m", provider_type="other",
                    window=1000, plan=[{"id": "a", "title": "T", "status": "pending", "note": ""}])
    ctx.extra["base_instructions"] = "SYS"
    out = cf._with_summary(ctx, "SYS")
    assert out.startswith("SYS") and cc.PLAN_HEADER in out and cc.SUMMARY_HEADER not in out


def test_with_summary_unchanged_when_no_summary_and_no_plan():
    ctx = rc.RunCtx(session_id="s", user_id="u", model_name="m", provider_type="other", window=1000)
    assert cf._with_summary(ctx, "SYS") == "SYS"


def test_db_plan_json_roundtrip(tmp_path):
    import db
    conn = init_db(str(tmp_path / "p.db"))
    conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) VALUES('s1','u1',0,0)"); conn.commit()
    assert db.get_plan_json(conn, "s1") == []
    db.set_plan_json(conn, "s1", [{"id": "a", "title": "T", "status": "pending", "note": ""}])
    assert db.get_plan_json(conn, "s1")[0]["id"] == "a"
    db.set_plan_json(conn, "s1", [])
    assert db.get_plan_json(conn, "s1") == []
    assert db.get_plan_json(conn, "missing") == []


@pytest.mark.asyncio
async def test_filter_injects_plan_even_when_compaction_disabled():
    from agents.run import CallModelData, ModelInputData
    ctx = rc.RunCtx(session_id="s", user_id="u", model_name="m", provider_type="other",
                    window=1000, compaction_enabled=False,
                    plan=[{"id": "a", "title": "T", "status": "pending", "note": ""}])
    ctx.extra["base_instructions"] = "SYS"
    rc.RUN_CTX_VAR.set(ctx)
    try:
        data = CallModelData(model_data=ModelInputData(input=[{"role": "user", "content": "hi"}], instructions="SYS"),
                             agent=None, context=None)
        out = await cf.compaction_filter(data)
        assert cc.PLAN_HEADER in (out.instructions or "")
        assert out.input == [{"role": "user", "content": "hi"}]
    finally:
        rc.RUN_CTX_VAR.set(None)
