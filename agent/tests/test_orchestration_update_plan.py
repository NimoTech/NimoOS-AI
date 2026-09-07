import asyncio
import json

import pytest

import run_context as rc
from db import init_db
from skills import orchestration as orch


class _Sink:
    def __init__(self): self.events = []
    async def put(self, e): self.events.append(e)


def _ctx(conn=None, sink=None, **kw):
    ctx = rc.RunCtx(session_id="s1", user_id="u1", model_name="m", provider_type="other",
                    window=1000, conn=conn, sink=sink, **kw)
    rc.RUN_CTX_VAR.set(ctx)
    return ctx


@pytest.fixture(autouse=True)
def _reset():
    yield
    rc.RUN_CTX_VAR.set(None)


def test_tool_is_registered_as_core():
    from skills import ALL_TOOLS
    from skills import tool_registry as reg
    names = {getattr(t, "name", "") for t in ALL_TOOLS}
    assert "update_plan" in names and "update_plan" in reg.CORE_TOOL_NAMES


@pytest.mark.asyncio
async def test_update_plan_stores_persists_and_emits(tmp_path):
    conn = init_db(str(tmp_path / "p.db"))
    conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) VALUES('s1','u1',0,0)"); conn.commit()
    sink = _Sink()
    ctx = _ctx(conn=conn, sink=sink)
    out = await orch._update_plan_impl([{"id": "1", "title": "Read", "status": "done"},
                                        {"id": "2", "title": "Collect", "status": "in_progress"}])
    data = json.loads(out)
    assert data["status"] == "ok" and data["steps"] == 2
    assert ctx.plan[1]["status"] == "in_progress" and ctx.plan[0]["note"] == ""
    import db
    assert db.get_plan_json(conn, "s1")[0]["id"] == "1"
    assert sink.events == [{"type": "plan_updated", "steps": ctx.plan}]


@pytest.mark.asyncio
async def test_update_plan_rejects_bad_payload_without_touching_state():
    ctx = _ctx(plan=[{"id": "a", "title": "T", "status": "pending", "note": ""}])
    out = await orch._update_plan_impl([{"id": "x", "title": "y", "status": "doing"}])
    assert json.loads(out)["error"].startswith("step x: status")
    assert ctx.plan[0]["id"] == "a"


@pytest.mark.asyncio
async def test_update_plan_without_ctx_or_in_child_is_refused():
    rc.RUN_CTX_VAR.set(None)
    assert "error" in json.loads(await orch._update_plan_impl([]))
    _ctx(depth=1)
    assert "not available" in json.loads(await orch._update_plan_impl([]))["error"]


@pytest.mark.asyncio
async def test_update_plan_empty_list_clears_plan(tmp_path):
    conn = init_db(str(tmp_path / "p.db"))
    conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at,plan_json) VALUES('s1','u1',0,0,'[{\"id\":\"a\"}]')"); conn.commit()
    ctx = _ctx(conn=conn, sink=_Sink(), plan=[{"id": "a", "title": "T", "status": "pending", "note": ""}])
    await orch._update_plan_impl([])
    import db
    assert ctx.plan == [] and db.get_plan_json(conn, "s1") == []
