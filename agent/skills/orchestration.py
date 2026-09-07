"""Orchestration core tools (spec §7): update_plan (pinned checklist) and
delegate (bounded sub-agent run). Both read the per-run RunCtx; neither takes
identity parameters."""
from __future__ import annotations

import json
import logging

from agents import function_tool

import context_compaction as cc
import run_context as rc

_LOG = logging.getLogger("nimoos-agent.orchestration")


def _err(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


async def _update_plan_impl(steps) -> str:
    ctx = rc.current()
    if ctx is None:
        return _err("no run context")
    if ctx.depth > 0:
        return _err("update_plan is not available inside a delegated sub-agent; report your findings instead")
    try:
        plan = cc.validate_plan(steps)
    except ValueError as exc:
        return _err(str(exc))
    ctx.plan = plan
    if ctx.conn is not None:
        try:
            import db as _db  # noqa: PLC0415
            _db.set_plan_json(ctx.conn, ctx.session_id, plan)
        except Exception:  # noqa: BLE001 — persistence is best-effort
            _LOG.warning("update_plan: persist failed", exc_info=True)
    if ctx.sink is not None:
        try:
            await ctx.sink.put({"type": "plan_updated", "steps": plan})
        except Exception:  # noqa: BLE001
            _LOG.debug("update_plan: emit failed", exc_info=True)
    return json.dumps({"status": "ok", "steps": len(plan)}, ensure_ascii=False)


@function_tool
async def update_plan(steps_json: str) -> str:
    """Replace your whole working plan and show it to the user as a checklist.
    Call it before starting any task with more than three steps, and again
    whenever a step changes status. `steps_json` is a JSON array of
    {"id": "1", "title": "...", "status": "pending" | "in_progress" | "done"
    | "skipped", "note": "optional short result"}. Max 30 steps, 200 chars per
    field. Pass "[]" to clear the plan. The current plan is always visible to
    you in a <plan> block."""
    try:
        steps = json.loads(steps_json)
    except (TypeError, ValueError) as exc:
        return _err(f"steps_json must be a JSON array: {exc}")
    return await _update_plan_impl(steps)


ORCHESTRATION_TOOLS = [update_plan]
