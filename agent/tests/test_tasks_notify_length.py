"""Long task output must survive the notification, not get cut at 800 chars.

`format_message` truncated the run summary to 800 characters with a hard slice
and no ellipsis. That ceiling was ours, not the channel's: Feishu accepts far
more, and any real report — a daily digest of eight items, a build log tail, a
migration summary — is several thousand characters. The user saw a report that
simply stopped mid-sentence, with nothing saying it had been cut.

Two rules, and the second is the one that keeps it honest:

* the limit comes from the CHANNEL (Telegram 4096, Discord 2000, Lark ~10k), so
  we never send something the platform will reject outright;
* anything over that is SPLIT across a bounded number of messages, and if it
  still does not fit, the last part says so. A digest that silently loses its
  tail is worse than one that admits it — the reader cannot tell the difference
  between "nothing more happened" and "the rest was dropped".
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

from tasks import notify


# ── per-channel limits ────────────────────────────────────────────────────────

def test_each_supported_channel_has_a_limit():
    for channel in ("telegram", "discord", "lark"):
        assert notify.channel_limit(channel) > 0


def test_telegram_and_discord_use_their_real_platform_caps():
    # These are hard API limits; exceeding them is a rejected send, not a
    # truncated one.
    assert notify.channel_limit("telegram") <= 4096
    assert notify.channel_limit("discord") <= 2000


def test_lark_allows_far_more_than_the_old_800_ceiling():
    assert notify.channel_limit("lark") >= 4000


def test_an_unknown_channel_falls_back_to_the_smallest_limit():
    # A new adapter must not inherit "unlimited" by being unlisted; the safe
    # default is the tightest cap we know of.
    unknown = notify.channel_limit("some-future-platform")
    assert unknown <= min(notify.channel_limit(c)
                          for c in ("telegram", "discord", "lark"))


# ── splitting ────────────────────────────────────────────────────────────────

def test_a_short_message_is_a_single_part_and_is_unchanged():
    assert notify.split_message("hello", limit=100) == ["hello"]


def test_a_long_message_is_split_into_several_parts():
    parts = notify.split_message("x" * 250, limit=100)
    assert len(parts) > 1
    assert all(len(p) <= 100 for p in parts)


def test_splitting_loses_no_content_when_it_fits_in_the_allowed_parts():
    body = "\n".join(f"line {i}" for i in range(50))
    parts = notify.split_message(body, limit=120, max_parts=10)
    assert "".join(parts).replace("\n", "") == body.replace("\n", "")


def test_a_split_prefers_line_boundaries():
    # A digest is a list of entries; cutting mid-line makes it unreadable.
    body = "\n".join(f"{i}. an entry about something" for i in range(1, 11))
    parts = notify.split_message(body, limit=90, max_parts=10)
    assert len(parts) > 1
    for part in parts[:-1]:
        assert not part.endswith(" "), part
        # every part should end at a completed line
        assert part.splitlines()[-1] in body.splitlines(), part


def test_a_single_line_longer_than_the_limit_is_still_split():
    # No line boundary to use: it must not return an over-limit part.
    parts = notify.split_message("y" * 300, limit=100, max_parts=10)
    assert all(len(p) <= 100 for p in parts)
    assert len(parts) == 3


def test_output_beyond_the_part_budget_is_marked_as_truncated():
    parts = notify.split_message("z" * 10_000, limit=100, max_parts=2)
    assert len(parts) == 2
    assert notify.TRUNCATION_NOTE in parts[-1]


def test_a_message_that_exactly_fits_is_not_marked_truncated():
    parts = notify.split_message("z" * 200, limit=100, max_parts=2)
    assert len(parts) == 2
    assert notify.TRUNCATION_NOTE not in parts[-1]


def test_an_empty_message_yields_nothing_to_send():
    assert notify.split_message("", limit=100) == []


# ── the formatter no longer imposes its own ceiling ───────────────────────────

def _rows(summary: str):
    task = {"name": "daily digest", "notify_policy": "always",
            "notify_channel": "", "notify_on_start": 0}
    run = {"status": "succeeded", "summary": summary, "error": "",
           "denied_actions": "[]", "started_at": 100, "finished_at": 142}
    return task, run


def test_a_long_summary_is_no_longer_cut_at_800_characters():
    task, run = _rows("A" * 5000)
    text = notify.format_message(task, run)
    assert text.count("A") == 5000


def test_the_formatter_still_caps_absurd_output():
    # Not "unlimited": a runaway run must not try to push a megabyte through a
    # chat adapter. The cap is generous enough that real reports never see it.
    task, run = _rows("B" * (notify.SUMMARY_MAX_CHARS + 5000))
    text = notify.format_message(task, run)
    assert len(text) <= notify.SUMMARY_MAX_CHARS + 500


def test_the_generous_cap_is_far_above_the_old_ceiling():
    assert notify.SUMMARY_MAX_CHARS >= 8000


# ── the send path actually splits ─────────────────────────────────────────────
#
# Without this wiring the two halves are useless on their own: the formatter now
# emits up to SUMMARY_MAX_CHARS, and handing 20k characters to a Telegram
# adapter is a REJECTED send — i.e. raising the ceiling alone would turn a
# truncated notification into no notification at all.

import asyncio

import db as db_module
from channels import store as channel_store
from tasks import store as task_store


NOW_MS = 1_800_000_000_000


def _paired_channel(conn, channel_type="lark"):
    """instance + binding + chat — the full shape `_resolve_target` requires.

    Written as raw INSERTs for the same reason test_tasks_notify.py's `_pair`
    is: `channel_chats` is populated lazily on a chat's first real message, not
    at pairing time, so there is no single store call that produces this shape.
    """
    instance_id, chat_id = f"inst-{channel_type}", "chat-1"
    conn.execute(
        "INSERT INTO channel_instances (id, channel_type, name, config_json, "
        "enabled, created_by, created_at, updated_at) VALUES (?,?,?,?,1,?,?,?)",
        (instance_id, channel_type, "bot", "{}", "u1", NOW_MS, NOW_MS))
    binding_id = f"bind-{instance_id}"
    conn.execute(
        "INSERT INTO channel_bindings (id, instance_id, external_user_id, "
        "external_username, user_id, revoked, created_at) "
        "VALUES (?,?,?,?,?,0,?)",
        (binding_id, instance_id, "x1", "who", "u1", NOW_MS))
    channel_store.upsert_chat(conn, instance_id, chat_id, binding_id,
                              "sess-x", NOW_MS)
    conn.commit()
    return f"{instance_id}:{chat_id}"


def test_a_long_report_is_delivered_as_several_messages(tmp_path):
    conn = db_module.init_db(str(tmp_path / "t.db"))
    channel = _paired_channel(conn, "discord")        # tightest cap: 2000
    task_id = task_store.create_task(
        conn, "u1", name="digest", prompt="p", trigger_type="cron",
        cron_expr="0 9 * * *", notify_policy="always", notify_channel=channel)
    task = task_store.get_task(conn, task_id, "u1")

    run = {"status": "succeeded", "summary": "L" * 5000, "error": "",
           "denied_actions": "[]", "started_at": 1, "finished_at": 2}

    sent = []

    async def fake_send(instance_id, chat_id, text):
        sent.append(text)
        return True

    assert asyncio.run(notify.send_result(conn, task, run, sender=fake_send))
    assert len(sent) > 1, "5000 chars must not go to Discord as one message"
    assert all(len(t) <= notify.channel_limit("discord") for t in sent)
    assert "".join(sent).count("L") >= 4000


def test_a_short_report_is_still_exactly_one_message(tmp_path):
    conn = db_module.init_db(str(tmp_path / "t.db"))
    channel = _paired_channel(conn, "lark")
    task_id = task_store.create_task(
        conn, "u1", name="digest", prompt="p", trigger_type="cron",
        cron_expr="0 9 * * *", notify_policy="always", notify_channel=channel)
    task = task_store.get_task(conn, task_id, "u1")
    run = {"status": "succeeded", "summary": "short", "error": "",
           "denied_actions": "[]", "started_at": 1, "finished_at": 2}

    sent = []

    async def fake_send(instance_id, chat_id, text):
        sent.append(text)
        return True

    assert asyncio.run(notify.send_result(conn, task, run, sender=fake_send))
    assert len(sent) == 1


def test_a_failure_on_a_later_part_still_reports_partial_delivery(tmp_path):
    """One rejected part must not make the whole notification look unsent.

    The reader already has part 1 in their chat; returning False would tell the
    runner nothing was delivered, and the log would disagree with the chat.
    """
    conn = db_module.init_db(str(tmp_path / "t.db"))
    channel = _paired_channel(conn, "discord")
    task_id = task_store.create_task(
        conn, "u1", name="digest", prompt="p", trigger_type="cron",
        cron_expr="0 9 * * *", notify_policy="always", notify_channel=channel)
    task = task_store.get_task(conn, task_id, "u1")
    run = {"status": "succeeded", "summary": "M" * 5000, "error": "",
           "denied_actions": "[]", "started_at": 1, "finished_at": 2}

    calls = {"n": 0}

    async def flaky_send(instance_id, chat_id, text):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("adapter blew up")
        return True

    assert asyncio.run(notify.send_result(conn, task, run, sender=flaky_send))
    assert calls["n"] >= 2


def test_nothing_is_sent_when_every_part_fails(tmp_path):
    conn = db_module.init_db(str(tmp_path / "t.db"))
    channel = _paired_channel(conn, "lark")
    task_id = task_store.create_task(
        conn, "u1", name="digest", prompt="p", trigger_type="cron",
        cron_expr="0 9 * * *", notify_policy="always", notify_channel=channel)
    task = task_store.get_task(conn, task_id, "u1")
    run = {"status": "succeeded", "summary": "ok", "error": "",
           "denied_actions": "[]", "started_at": 1, "finished_at": 2}

    async def dead_send(instance_id, chat_id, text):
        return False

    assert asyncio.run(notify.send_result(conn, task, run, sender=dead_send)) is False
