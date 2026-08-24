"""Agent tool: create a scheduled task from chat (spec §2 M5).

HARD CONSTRAINT (spec §6 red line): a task created by the agent is DISABLED
with an EMPTY preauth document — the tool accepts neither ``preauth`` nor
``enabled``, so "agent 自建 = 自授权" is structurally impossible.  Authorizing
and enabling happen only in the authenticated UI (AI → Tasks, /ai/tasks).
"""
from __future__ import annotations

from agents import function_tool

# Mirrors main._MIN_INTERVAL_SECONDS (a module-level import of main would be
# circular; the API enforces the same floor for UI-created tasks).
MIN_INTERVAL_SECONDS = 60

# A prompt-injected loop must not be able to spam thousands of dormant rows.
MAX_TASKS_PER_USER = 50

_UI_HINT = ("It is DISABLED and has no permissions yet. Ask the user to open "
            "AI → Tasks (/ai/tasks) to review the prompt, grant "
            "pre-authorizations, and enable it.")


async def _create_scheduled_task_impl(name: str, prompt: str,
                                      cron_expr: str = "",
                                      interval_seconds: int = 0) -> str:
    import db as _db
    from skills.skills_registry import USER_ID_VAR
    from tasks import store as _store

    user_id = USER_ID_VAR.get()
    if not user_id:
        return "Cannot create a task without a user identity."
    name = (name or "").strip()
    prompt = (prompt or "").strip()
    if not name or not prompt:
        return "Both name and prompt are required."
    cron_expr = (cron_expr or "").strip()
    try:
        interval_seconds = int(interval_seconds or 0)
    except (TypeError, ValueError):
        return "interval_seconds must be an integer."

    if cron_expr and interval_seconds > 0:
        return "Pass either cron_expr or interval_seconds, not both."
    if cron_expr:
        import time
        from tasks import cron as _cron
        try:
            _cron.validate(cron_expr)
            # `validate` only parses the fields; `0 0 30 2 *` parses fine and
            # never fires, and create_task would raise computing next_run_at.
            _cron.next_after(cron_expr, int(time.time()))
        except Exception:  # noqa: BLE001 — CronError/ValueError/TypeError
            return f"Invalid cron expression: {cron_expr!r} (5 fields)."
        trigger = "cron"
    elif interval_seconds > 0:
        if interval_seconds < MIN_INTERVAL_SECONDS:
            return f"interval_seconds must be >= {MIN_INTERVAL_SECONDS}."
        trigger = "interval"
    else:
        trigger = "webhook_only"

    conn = _db.get_connection()
    count = conn.execute(
        "SELECT COUNT(*) FROM scheduled_tasks WHERE user_id=?",
        (user_id,)).fetchone()[0]
    if count >= MAX_TASKS_PER_USER:
        return (f"Too many tasks ({count}); the limit is "
                f"{MAX_TASKS_PER_USER}. Ask the user to delete some first.")

    task_id = _store.create_task(
        conn, user_id, name=name, prompt=prompt, trigger_type=trigger,
        cron_expr=cron_expr, interval_seconds=interval_seconds, enabled=0)
    return f"Created scheduled task '{name}' (id {task_id}). {_UI_HINT}"


@function_tool
async def create_scheduled_task(name: str, prompt: str, cron_expr: str = "",
                                interval_seconds: int = 0) -> str:
    """Create a scheduled agent task. The task starts DISABLED with no
    pre-authorizations; the user must review, authorize and enable it on the
    Tasks page (AI → Tasks). Never claim the task will run — it will not
    until the user enables it.

    Args:
        name: short display name (e.g. "Daily Feishu digest").
        prompt: the full self-contained instruction the task will run with.
        cron_expr: optional 5-field cron schedule (e.g. "0 9 * * *").
        interval_seconds: optional fixed interval; >= 60. Pass NEITHER
            schedule field for a task triggered only manually or by webhook.
    """
    return await _create_scheduled_task_impl(
        name=name, prompt=prompt, cron_expr=cron_expr,
        interval_seconds=interval_seconds)


ALL_TOOLS = [create_scheduled_task]
