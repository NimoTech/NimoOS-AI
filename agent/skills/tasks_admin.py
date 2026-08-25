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
            "pre-authorizations, enable it, and pick a notify channel "
            "(Feishu/Telegram/…) so the result actually reaches them — the "
            "runner delivers the task's final answer through that channel.")


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

    DELIVERY RULE (write the prompt accordingly): the task runner delivers
    the run's FINAL ANSWER to the user through the task's notify channel
    (Feishu/Telegram/…, configured on the Tasks page). So the prompt must
    make the final answer BE the content to deliver. NEVER write steps like
    "send the result via lark-cli" or "message the user" into a task prompt:
    task runs are non-interactive, message-sending CLIs there run under the
    USER identity and require an OAuth scope grant that can never be
    completed inside a finished run — the task would loop asking for
    authorization forever instead of delivering anything.

    Args:
        name: short display name (e.g. "Daily Feishu digest").
        prompt: the full self-contained instruction the task will run with.
            End it with what the final answer should contain — that answer is
            what gets delivered.
        cron_expr: optional 5-field cron schedule (e.g. "0 9 * * *").
        interval_seconds: optional fixed interval; >= 60. Pass NEITHER
            schedule field for a task triggered only manually or by webhook.
    """
    return await _create_scheduled_task_impl(
        name=name, prompt=prompt, cron_expr=cron_expr,
        interval_seconds=interval_seconds)


# Prompt revisions are capped like a hand-written prompt would be; a runaway
# model must not persist a novel into the task row.
PROMPT_MAX_CHARS = 8000


async def _update_task_prompt_impl(new_prompt: str, reason: str = "") -> str:
    import time

    import db as _db
    from skills.skills_registry import USER_ID_VAR
    from skills.tool_gating import GATING_SESSION_VAR

    user_id = USER_ID_VAR.get()
    session_id = GATING_SESSION_VAR.get("")
    if not user_id or not session_id:
        return "Cannot revise a task prompt without a run identity."

    new_prompt = (new_prompt or "").strip()
    if not new_prompt:
        return "new_prompt is required."
    if len(new_prompt) > PROMPT_MAX_CHARS:
        return (f"new_prompt is too long ({len(new_prompt)} chars; the limit "
                f"is {PROMPT_MAX_CHARS}).")

    conn = _db.get_connection()
    # The binding is derived, never taken from the model: this session must be
    # the one a RUNNING CONTINUATION run points at, and the tool can only ever
    # touch the task that owns that run. In chat, or in a plain scheduled run,
    # there is no such row and the tool refuses.
    row = conn.execute(
        "SELECT task_id FROM task_runs WHERE session_id=? "
        "AND status='running' AND resumed_from!='' LIMIT 1",
        (session_id,)).fetchone()
    if row is None:
        return ("This tool only works inside a CONTINUATION run of a "
                "scheduled task (the user pressed Continue on a finished "
                "run). It cannot be used from chat or a normal scheduled "
                "run.")
    task = conn.execute(
        "SELECT * FROM scheduled_tasks WHERE id=? AND user_id=?",
        (row["task_id"], user_id)).fetchone()
    if task is None:
        return "The task this run belongs to no longer exists."
    old_prompt = task["prompt"]
    if new_prompt == old_prompt:
        return "The new prompt is identical to the current one; nothing to do."

    now = int(time.time())
    conn.execute(
        "UPDATE scheduled_tasks SET prompt=?, prev_prompt=?, "
        "prompt_revised_at=?, prompt_revised_by='agent', updated_at=? "
        "WHERE id=? AND user_id=?",
        (new_prompt, old_prompt, now, now, task["id"], user_id))
    conn.commit()
    suffix = f" Reason: {reason.strip()}" if (reason or "").strip() else ""
    return (f"Revised the prompt of task '{task['name']}'. The previous "
            f"version was kept and the user can revert it on the Tasks "
            f"page.{suffix}")


@function_tool
async def update_task_prompt(new_prompt: str, reason: str = "") -> str:
    """Revise the prompt of the scheduled task this CONTINUATION run belongs
    to, so future scheduled runs do not repeat a failure you just diagnosed.
    Only available inside a continuation run (the user pressed Continue on a
    finished run); it always targets that run's own task — no other.

    It changes ONLY the prompt text. It cannot enable/disable the task, grant
    permissions, or change its schedule or notify settings. The previous
    prompt is kept and the user can revert the revision on the Tasks page —
    mention the revision in your final answer.

    Args:
        new_prompt: the full replacement prompt (self-contained, ends with
            what the final answer should contain — that answer is what gets
            delivered through the notify channel).
        reason: one short sentence on what was wrong with the old prompt.
    """
    return await _update_task_prompt_impl(new_prompt=new_prompt, reason=reason)


ALL_TOOLS = [create_scheduled_task, update_task_prompt]
