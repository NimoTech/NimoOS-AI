"""notify — result/failure notifications for scheduled tasks (M2 task 6).

`send_result` is the whole story: decide (from `notify_policy`) whether this
run is worth telling a human about, render the text, and hand it to a paired
channel adapter. Nothing in here may raise past `send_result` — a scheduled
run has nobody watching, and `tasks/runner.py` calls this from its `finally`
block specifically so a broken notification can never touch the run result
that `store.finish_run` already committed.

**`notify_channel`'s real format is `<instance_id>:<external_chat_id>`, not
`<channel_type>:<chat_id>`.** The brief assumed the latter; the actual channel
stack (`channels/manager.py`, `channels/store.py`) rules it out on both
halves:

* Routing needs the *instance*, not just the type. `ChannelManager` keeps one
  running adapter per `channel_instances.id` (`self._running: instance_id ->
  (adapter, fingerprint)`), and nothing stops two instances from sharing a
  `channel_type` (two Telegram bots). `channel_type` alone cannot pick one.
* The addressable unit is `external_chat_id`, not the bound user's
  `external_user_id`. For Telegram the two happen to be equal in a private
  chat, but Discord's `external_chat_id` is `message.channel.id` — the DM
  channel's own snowflake, distinct from `message.author.id`
  (`channels/discord.py:63-64`). Only `channel_chats` (keyed on
  `instance_id, external_chat_id`) ties a chat back to a `binding_id`; that
  row is written lazily, on the chat's first non-command message
  (`channels/router.py`'s `_run_serialized`/`_cmd_new`), not at `/pair` time.

So resolution here is: parse `<instance_id>:<external_chat_id>`, require a
`channel_chats` row for that exact pair (implies the chat has talked to the
bot at least once — a real precondition for Task 7/8's UI to surface, see the
report), and require its `channel_bindings` owner to still match the task's
`user_id` (revoked/repointed bindings must not receive someone else's task
notifications). Only then is the paired adapter looked up and sent to.

Task 7/8: build the "already paired channel" picker from `channel_chats`
joined to `channel_bindings` for the current user (there is no ready-made
store helper for that reverse listing yet — `list_bindings_for_user` alone
lacks `external_chat_id`), and store the selection as `<instance_id>:<chat>`.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("nimoos-agent.tasks")

# "summary 前 800 字" — a hard character cut, no ellipsis; the run history
# itself still has the full text, this is only the chat-side digest.
_SUMMARY_MAX_CHARS = 800

# "denied_actions 摘要(最多 5 条)" — same reasoning as runner.format_preauth_note:
# an unattended run that hammered a confirmation gate must not be able to
# blow up the notification text.
_DENIED_MAX_ITEMS = 5


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text[:limit]


def _denied_list(denied_actions) -> list:
    """`run_row['denied_actions']` is the store's JSON text column; a caller
    that already has a parsed list (e.g. a test fixture) is accepted too."""
    if isinstance(denied_actions, str):
        try:
            denied_actions = json.loads(denied_actions)
        except (TypeError, ValueError):
            return []
    return denied_actions if isinstance(denied_actions, list) else []


def _denied_summary(denied_actions) -> str:
    items = _denied_list(denied_actions)
    if not items:
        return ""
    lines = []
    for item in items[:_DENIED_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "unknown")
        detail = str(item.get("detail") or "").strip()
        lines.append(f"- {kind}: {detail}" if detail else f"- {kind}")
    remaining = len(items) - _DENIED_MAX_ITEMS
    if remaining > 0:
        lines.append(f"…and {remaining} more")
    return "\n".join(lines)


def format_message(task_row, run_row) -> str:
    """Render the chat text for one finished run.

    Deliberately English, not the Chinese literal in the brief's `失败`
    placeholder — every other user-facing channel string in this codebase
    (`channels/router.py`'s `MSG_*`) is English, and hardcoded Chinese in the
    agent was a whole cleanup project (see MEMORY: "Agent 硬编码中文清除").
    Reintroducing one here would undo that.
    """
    name = task_row["name"] or "(unnamed task)"
    status = run_row["status"]
    if status == "succeeded":
        summary = _truncate(str(run_row["summary"] or ""), _SUMMARY_MAX_CHARS)
        header = f"✅ {name}"
        return f"{header}\n\n{summary}" if summary else header

    # Only reachable under `always` (see `_should_notify`), and it must not
    # claim the run failed — nothing was attempted.
    if status == "skipped":
        header = f"⏭️ {name} skipped"
        reason = str(run_row["error"] or "").strip()
        return f"{header}\n\n{reason}" if reason else header

    header = f"⚠️ {name} failed"
    parts = [header]
    error = str(run_row["error"] or "").strip()
    if error:
        parts.append(error)
    denied_text = _denied_summary(run_row["denied_actions"])
    if denied_text:
        parts.append(f"Denied actions:\n{denied_text}")
    return "\n\n".join(parts)


# What `failure` means: the run was attempted and did not work. Deliberately
# NOT "anything that is not succeeded" — that also catches `skipped`, which
# `overlap_policy=skip` writes on EVERY fire while a slow run is still going.
# A task on a 1-minute schedule that takes 10 minutes then pushes ten "failed"
# notifications per run, and the useful failure notification drowns in them.
# A skip is not a failure: nothing was attempted and nothing is broken; the
# run history still records it. Users who want to see them opt in with
# `always`, which keeps meaning literally every terminal run.
_FAILURE_STATUSES = ("failed", "timeout")


def _should_notify(policy: str, status: str) -> bool:
    if policy == "always":
        return True
    if policy == "failure":
        return status in _FAILURE_STATUSES
    return False  # 'never', or anything unrecognized — degrade to silent


def _resolve_target(conn, task_row, raw_channel: str):
    """`<instance_id>:<external_chat_id>` -> (instance_id, chat_id), or None
    if the string is malformed, unpaired, or no longer owned by this task's
    user. See the module docstring for why this is not `<channel_type>:...`.
    """
    if not raw_channel or ":" not in raw_channel:
        return None
    instance_id, _, chat_id = raw_channel.partition(":")
    instance_id, chat_id = instance_id.strip(), chat_id.strip()
    if not instance_id or not chat_id:
        return None

    from channels import store as channel_store  # noqa: PLC0415

    chat = channel_store.get_chat(conn, instance_id, chat_id)
    if chat is None:
        return None
    binding = conn.execute(
        "SELECT user_id FROM channel_bindings WHERE id=? AND revoked=0",
        (chat["binding_id"],),
    ).fetchone()
    if binding is None or str(binding["user_id"]) != str(task_row["user_id"]):
        return None
    return instance_id, chat_id


async def _default_send(instance_id: str, chat_id: str, text: str) -> bool:
    """Production sender: the running adapter behind `instance_id`, via
    `main._channel_manager`. `ChannelManager` has no public getter for a
    single adapter (`reload()`/`start_all()`/`stop_all()` only) so this reaches
    into `_running` directly rather than adding one — this task's scope is
    `tasks/notify.py` + `tasks/runner.py` only.
    """
    try:
        import main  # noqa: PLC0415
    except Exception:                       # noqa: BLE001 — main not importable
        logger.warning("tasks notify: main not importable; cannot send",
                       exc_info=True)
        return False

    mgr = getattr(main, "_channel_manager", None)
    if mgr is None:
        return False
    running = getattr(mgr, "_running", None) or {}
    entry = running.get(instance_id)
    if entry is None:
        return False
    adapter = entry[0]

    from channels.model import OutboundMessage  # noqa: PLC0415

    try:
        result = await adapter.send(chat_id, OutboundMessage(text=text))
    except Exception:                       # noqa: BLE001 — never raise past send_result
        logger.warning("tasks notify: adapter.send failed (instance %s)",
                       instance_id, exc_info=True)
        return False
    # An adapter may report failure by RETURNING falsey rather than raising —
    # LarkAdapter's contract is "never raise, return falsey", so the except
    # clause above catches nothing for it. Note `""` is a SUCCESS (delivered,
    # id unparsable), so this must test `is None`, never truthiness.
    if result is None:
        logger.warning("tasks notify: adapter.send reported failure (instance %s)",
                       instance_id)
        return False
    return True


async def send_result(conn, task_row, run_row, *, sender=None) -> bool:
    """Notify the task owner about one finished run, if `notify_policy` says
    to. Returns whether a message was actually sent — never raises.

    `sender` is the test seam: `async (instance_id, chat_id, text) -> bool`,
    defaulting to `_default_send`.
    """
    policy = str(task_row["notify_policy"] or "failure")
    status = str(run_row["status"] or "")
    if not _should_notify(policy, status):
        return False

    raw_channel = str(task_row["notify_channel"] or "").strip()
    target = _resolve_target(conn, task_row, raw_channel)
    if target is None:
        return False
    instance_id, chat_id = target

    text = format_message(task_row, run_row)
    send = sender or _default_send
    try:
        ok = await send(instance_id, chat_id, text)
    except Exception:                       # noqa: BLE001 — a task run's result
        # must never depend on a channel adapter behaving; log and move on.
        logger.warning("tasks notify: send failed for task %s",
                       task_row["id"] if "id" in task_row.keys() else "?",
                       exc_info=True)
        return False
    return bool(ok)
