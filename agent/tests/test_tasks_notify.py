"""notify — result/failure notifications for scheduled tasks (M2 task 6).

Covers the policy matrix (never/failure/always x succeeded/non-succeeded),
message formatting (success/failure templates, 800-char summary cut, 5-item
denied-actions cap), `notify_channel` resolution against the REAL channel
tables (`channel_instances` / `channel_bindings` / `channel_chats` — see
`tasks/notify.py`'s module docstring for why the format is
`<instance_id>:<external_chat_id>`, not `<channel_type>:<chat_id>`), and the
"never raises" contract for both the injected sender seam and the real
`main._channel_manager`-backed default sender.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import json

import pytest

import db as db_module
import main
from channels import store as channel_store
from tasks import notify
from tasks import store as task_store

NOW_MS = 1_800_000_000_000


@pytest.fixture
def conn(tmp_path):
    return db_module.init_db(str(tmp_path / "t.db"))


# -- fixtures ------------------------------------------------------------

def _mk_task(conn, **over):
    kw = dict(name="daily digest", prompt="do the thing", trigger_type="cron",
             cron_expr="0 9 * * *", notify_policy="failure", notify_channel="")
    kw.update(over)
    return task_store.create_task(conn, "u1", **kw)


def _task_row(conn, task_id, user_id="u1"):
    return task_store.get_task(conn, task_id, user_id)


def _mk_run(conn, task_id, *, status="succeeded", summary="all good",
           error="", denied=None):
    run_id = task_store.create_run(conn, task_id, "u1", "cron")
    task_store.finish_run(conn, run_id, status, summary=summary, error=error,
                          denied=denied)
    return conn.execute(
        "SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()


def _pair(conn, *, instance_id="inst-1", chat_id="chat-1", user_id="u1",
         revoked=False):
    """Wire up instance + binding + chat — the full shape `_resolve_target`
    requires, mirroring what real pairing (`/pair`) + a first real message
    (`channel_chats` is written lazily, not at pairing time) produce."""
    conn.execute(
        "INSERT INTO channel_instances (id, channel_type, name, config_json, "
        "enabled, created_by, created_at, updated_at) VALUES (?,?,?,?,1,?,?,?)",
        (instance_id, "telegram", "bot", "{}", "u1", NOW_MS, NOW_MS))
    binding_id = f"bind-{instance_id}-{chat_id}"
    conn.execute(
        "INSERT INTO channel_bindings (id, instance_id, external_user_id, "
        "external_username, user_id, revoked, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (binding_id, instance_id, chat_id, "someone", user_id,
         1 if revoked else 0, NOW_MS))
    channel_store.upsert_chat(conn, instance_id, chat_id, binding_id,
                              "sess-x", NOW_MS)
    conn.commit()
    return instance_id, chat_id


class _FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, msg):
        self.sent.append((chat_id, msg.text))
        return "msg-id"


class _FakeManager:
    def __init__(self, running):
        self._running = running


# -- message formatting ---------------------------------------------------

def test_format_message_success_with_summary():
    task = {"name": "daily digest"}
    run = {"status": "succeeded", "summary": "all good", "error": "",
          "denied_actions": "[]"}
    assert notify.format_message(task, run) == "✅ daily digest\n\nall good"


def test_format_message_success_without_summary():
    task = {"name": "daily digest"}
    run = {"status": "succeeded", "summary": "", "error": "",
          "denied_actions": "[]"}
    assert notify.format_message(task, run) == "✅ daily digest"


def test_format_message_failure_includes_error_and_denied():
    task = {"name": "daily digest"}
    denied = [{"kind": "egress", "detail": "evil.example"},
             {"kind": "shell", "detail": "rm -rf /"}]
    run = {"status": "failed", "summary": "", "error": "boom",
          "denied_actions": json.dumps(denied)}
    text = notify.format_message(task, run)
    assert text.startswith("⚠️ daily digest failed")
    assert "boom" in text
    assert "egress: evil.example" in text
    assert "shell: rm -rf /" in text


def test_format_message_skipped_does_not_claim_failure():
    task = {"name": "daily digest"}
    run = {"status": "skipped", "summary": "", "denied_actions": "[]",
           "error": "previous run still queued or running (overlap_policy=skip)"}
    text = notify.format_message(task, run)
    assert text.startswith("\u23ed\ufe0f daily digest skipped")
    assert "failed" not in text


def test_format_message_failure_without_error_or_denied():
    task = {"name": "daily digest"}
    run = {"status": "timeout", "summary": "", "error": "",
          "denied_actions": "[]"}
    assert notify.format_message(task, run) == "⚠️ daily digest failed"


def test_denied_summary_caps_at_five_and_notes_the_remainder():
    task = {"name": "t"}
    denied = [{"kind": f"k{i}", "detail": f"d{i}"} for i in range(7)]
    run = {"status": "failed", "summary": "", "error": "",
          "denied_actions": json.dumps(denied)}
    text = notify.format_message(task, run)
    for i in range(5):
        assert f"k{i}: d{i}" in text
    assert "k5" not in text
    assert "k6" not in text
    assert "…and 2 more" in text


def test_summary_is_cut_to_800_chars():
    task = {"name": "t"}
    run = {"status": "succeeded", "summary": "x" * 950, "error": "",
          "denied_actions": "[]"}
    text = notify.format_message(task, run)
    body = text.split("\n\n", 1)[1]
    assert len(body) == 800
    assert body == "x" * 800


# -- policy matrix ---------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("policy,status,expect_sent", [
    ("never", "succeeded", False),
    ("never", "failed", False),
    ("failure", "succeeded", False),
    ("failure", "failed", True),
    ("failure", "timeout", True),
    # A skip is not a failure: overlap_policy=skip writes one on every fire
    # while a slow run is still going, so counting them as failures turns a
    # `failure` subscription into a notification storm.
    ("failure", "skipped", False),
    ("always", "succeeded", True),
    ("always", "failed", True),
    ("always", "skipped", True),   # `always` still means every terminal run
])
async def test_policy_matrix(conn, policy, status, expect_sent):
    instance_id, chat_id = _pair(conn)
    tid = _mk_task(conn, notify_policy=policy,
                  notify_channel=f"{instance_id}:{chat_id}")
    run = _mk_run(conn, tid, status=status,
                 summary="done" if status == "succeeded" else "",
                 error="" if status == "succeeded" else "boom")
    task = _task_row(conn, tid)

    sent = []

    async def fake_sender(iid, cid, text):
        sent.append((iid, cid, text))
        return True

    ok = await notify.send_result(conn, task, run, sender=fake_sender)
    assert ok is expect_sent
    assert bool(sent) is expect_sent
    if expect_sent:
        assert sent[0][0] == instance_id
        assert sent[0][1] == chat_id


# -- channel resolution: never raises, false on any miss --------------------

@pytest.mark.asyncio
async def test_no_channel_configured_returns_false_without_calling_sender(conn):
    tid = _mk_task(conn, notify_policy="always", notify_channel="")
    run = _mk_run(conn, tid)
    task = _task_row(conn, tid)

    async def fake_sender(*a):
        raise AssertionError("sender must not be called")

    assert await notify.send_result(conn, task, run, sender=fake_sender) is False


@pytest.mark.asyncio
async def test_malformed_channel_string_returns_false(conn):
    tid = _mk_task(conn, notify_policy="always", notify_channel="not-a-pair")
    run = _mk_run(conn, tid)
    task = _task_row(conn, tid)
    assert await notify.send_result(conn, task, run, sender=None) is False


@pytest.mark.asyncio
async def test_unknown_instance_or_chat_returns_false(conn):
    tid = _mk_task(conn, notify_policy="always",
                  notify_channel="ghost-instance:ghost-chat")
    run = _mk_run(conn, tid)
    task = _task_row(conn, tid)
    assert await notify.send_result(conn, task, run, sender=None) is False


@pytest.mark.asyncio
async def test_binding_owned_by_a_different_user_is_refused(conn):
    instance_id, chat_id = _pair(conn, user_id="someone-else")
    tid = _mk_task(conn, notify_policy="always",
                  notify_channel=f"{instance_id}:{chat_id}")
    run = _mk_run(conn, tid)
    task = _task_row(conn, tid)

    called = []

    async def fake_sender(*a):
        called.append(a)
        return True

    assert await notify.send_result(conn, task, run, sender=fake_sender) is False
    assert called == []


@pytest.mark.asyncio
async def test_revoked_binding_is_refused(conn):
    instance_id, chat_id = _pair(conn, revoked=True)
    tid = _mk_task(conn, notify_policy="always",
                  notify_channel=f"{instance_id}:{chat_id}")
    run = _mk_run(conn, tid)
    task = _task_row(conn, tid)
    assert await notify.send_result(conn, task, run, sender=None) is False


@pytest.mark.asyncio
async def test_sender_exception_is_swallowed(conn):
    instance_id, chat_id = _pair(conn)
    tid = _mk_task(conn, notify_policy="always",
                  notify_channel=f"{instance_id}:{chat_id}")
    run = _mk_run(conn, tid)
    task = _task_row(conn, tid)

    async def boom(*a):
        raise RuntimeError("channel down")

    assert await notify.send_result(conn, task, run, sender=boom) is False


# -- default sender: main._channel_manager wiring ---------------------------

@pytest.mark.asyncio
async def test_default_sender_uses_the_running_adapter(conn, monkeypatch):
    instance_id, chat_id = _pair(conn)
    adapter = _FakeAdapter()
    monkeypatch.setattr(main, "_channel_manager",
                        _FakeManager({instance_id: (adapter, "{}")}))
    tid = _mk_task(conn, notify_policy="always",
                  notify_channel=f"{instance_id}:{chat_id}")
    run = _mk_run(conn, tid, status="succeeded", summary="all good")
    task = _task_row(conn, tid)

    assert await notify.send_result(conn, task, run) is True
    assert adapter.sent == [(chat_id, "✅ daily digest\n\nall good")]


@pytest.mark.asyncio
async def test_default_sender_no_channel_manager_returns_false(conn, monkeypatch):
    instance_id, chat_id = _pair(conn)
    monkeypatch.setattr(main, "_channel_manager", None)
    tid = _mk_task(conn, notify_policy="always",
                  notify_channel=f"{instance_id}:{chat_id}")
    run = _mk_run(conn, tid)
    task = _task_row(conn, tid)

    assert await notify.send_result(conn, task, run) is False


@pytest.mark.asyncio
async def test_default_sender_instance_not_running_returns_false(conn, monkeypatch):
    instance_id, chat_id = _pair(conn)
    monkeypatch.setattr(main, "_channel_manager", _FakeManager({}))
    tid = _mk_task(conn, notify_policy="always",
                  notify_channel=f"{instance_id}:{chat_id}")
    run = _mk_run(conn, tid)
    task = _task_row(conn, tid)

    assert await notify.send_result(conn, task, run) is False


@pytest.mark.asyncio
async def test_default_sender_adapter_send_failure_returns_false(conn, monkeypatch):
    instance_id, chat_id = _pair(conn)

    class _RaisingAdapter:
        async def send(self, chat_id, msg):
            raise RuntimeError("network down")

    monkeypatch.setattr(main, "_channel_manager",
                        _FakeManager({instance_id: (_RaisingAdapter(), "{}")}))
    tid = _mk_task(conn, notify_policy="always",
                  notify_channel=f"{instance_id}:{chat_id}")
    run = _mk_run(conn, tid)
    task = _task_row(conn, tid)

    assert await notify.send_result(conn, task, run) is False


@pytest.mark.asyncio
async def test_default_sender_treats_a_none_return_as_failure(conn, monkeypatch):
    """`LarkAdapter.send` never raises — it reports failure by returning None.
    Discarding the return recorded a send that never happened as delivered."""
    instance_id, chat_id = _pair(conn)

    class _SilentlyFailingAdapter:
        async def send(self, chat_id, msg):
            return None

    monkeypatch.setattr(main, "_channel_manager",
                        _FakeManager({instance_id: (_SilentlyFailingAdapter(),
                                                    "{}")}))
    tid = _mk_task(conn, notify_policy="always",
                  notify_channel=f"{instance_id}:{chat_id}")
    run = _mk_run(conn, tid)
    task = _task_row(conn, tid)

    assert await notify.send_result(conn, task, run) is False


@pytest.mark.asyncio
async def test_default_sender_treats_an_empty_string_return_as_success(
        conn, monkeypatch):
    """`""` means "delivered, but the message id could not be parsed" (see
    `LarkAdapter.send`). It is a SUCCESS — the failure check must be `is None`,
    never a truthiness test."""
    instance_id, chat_id = _pair(conn)

    class _IdlessAdapter:
        async def send(self, chat_id, msg):
            return ""

    monkeypatch.setattr(main, "_channel_manager",
                        _FakeManager({instance_id: (_IdlessAdapter(), "{}")}))
    tid = _mk_task(conn, notify_policy="always",
                  notify_channel=f"{instance_id}:{chat_id}")
    run = _mk_run(conn, tid)
    task = _task_row(conn, tid)

    assert await notify.send_result(conn, task, run) is True


@pytest.mark.asyncio
async def test_default_sender_treats_a_message_id_return_as_success(
        conn, monkeypatch):
    """What Telegram/Discord actually return on a successful send."""
    instance_id, chat_id = _pair(conn)

    class _IdReturningAdapter:
        async def send(self, chat_id, msg):
            return "om_abc123"

    monkeypatch.setattr(main, "_channel_manager",
                        _FakeManager({instance_id: (_IdReturningAdapter(),
                                                    "{}")}))
    tid = _mk_task(conn, notify_policy="always",
                  notify_channel=f"{instance_id}:{chat_id}")
    run = _mk_run(conn, tid)
    task = _task_row(conn, tid)

    assert await notify.send_result(conn, task, run) is True


# -- start notifications ---------------------------------------------------
# A run that takes ten minutes used to be indistinguishable from one that never
# started. `notify_on_start` is a per-task opt-in, deliberately independent of
# `notify_policy` — someone who only wants to hear about failures may still want
# to know the nightly job began — with one exception pinned below: `never` is a
# master mute, because a user who asked for silence must get silence.

def test_format_start_message():
    task = {"name": "daily digest"}
    run = {"trigger": "cron"}
    assert notify.format_start_message(task, run) == "▶️ daily digest started (cron)"


def test_format_start_message_without_a_trigger():
    assert notify.format_start_message({"name": "daily digest"}, {}) == \
        "▶️ daily digest started"


@pytest.mark.asyncio
async def test_send_start_requires_the_opt_in(conn):
    inst, chat = _pair(conn)
    task_id = _mk_task(conn, notify_policy="always",
                       notify_channel=f"{inst}:{chat}")
    run = _mk_run(conn, task_id)
    sent = []

    async def sender(i, c, text):
        sent.append(text)
        return True

    assert await notify.send_start(conn, _task_row(conn, task_id), run,
                                   sender=sender) is False
    assert sent == []


@pytest.mark.asyncio
async def test_send_start_sends_when_opted_in(conn):
    inst, chat = _pair(conn)
    task_id = _mk_task(conn, notify_policy="failure", notify_on_start=1,
                       notify_channel=f"{inst}:{chat}")
    run = _mk_run(conn, task_id)
    sent = []

    async def sender(i, c, text):
        sent.append((i, c, text))
        return True

    assert await notify.send_start(conn, _task_row(conn, task_id), run,
                                   sender=sender) is True
    assert len(sent) == 1
    assert sent[0][0] == inst and sent[0][1] == chat
    assert sent[0][2].startswith("▶️ daily digest started")


@pytest.mark.asyncio
async def test_never_mutes_the_start_message_too(conn):
    """`never` is the master mute: a contradictory config must not spam."""
    inst, chat = _pair(conn)
    task_id = _mk_task(conn, notify_policy="never", notify_on_start=1,
                       notify_channel=f"{inst}:{chat}")
    run = _mk_run(conn, task_id)
    sent = []

    async def sender(i, c, text):
        sent.append(text)
        return True

    assert await notify.send_start(conn, _task_row(conn, task_id), run,
                                   sender=sender) is False
    assert sent == []


@pytest.mark.asyncio
async def test_send_start_without_a_channel_is_silent(conn):
    task_id = _mk_task(conn, notify_on_start=1, notify_channel="")
    run = _mk_run(conn, task_id)
    called = []

    async def sender(i, c, text):
        called.append(text)
        return True

    assert await notify.send_start(conn, _task_row(conn, task_id), run,
                                   sender=sender) is False
    assert called == []


@pytest.mark.asyncio
async def test_send_start_swallows_a_broken_sender(conn):
    """Same contract as send_result: a run must never depend on a channel."""
    inst, chat = _pair(conn)
    task_id = _mk_task(conn, notify_on_start=1, notify_channel=f"{inst}:{chat}")
    run = _mk_run(conn, task_id)

    async def sender(i, c, text):
        raise RuntimeError("channel down")

    assert await notify.send_start(conn, _task_row(conn, task_id), run,
                                   sender=sender) is False


# -- completion message: what actually happened ----------------------------

def test_format_message_success_reports_the_duration():
    task = {"name": "daily digest"}
    run = {"status": "succeeded", "summary": "all good", "error": "",
           "denied_actions": "[]", "started_at": 1000, "finished_at": 1042}
    assert notify.format_message(task, run) == \
        "✅ daily digest (42s)\n\nall good"


def test_format_message_success_still_lists_denied_actions():
    """A run can succeed having been refused everything it tried; without this
    the message reads like the work got done."""
    task = {"name": "daily digest"}
    denied = [{"kind": "shell", "detail": "lark-cli im +messages-send"}]
    run = {"status": "succeeded", "summary": "I could not send it", "error": "",
           "denied_actions": json.dumps(denied),
           "started_at": 1000, "finished_at": 1005}
    text = notify.format_message(task, run)
    assert "Denied actions:" in text
    assert "shell: lark-cli im +messages-send" in text


def test_format_message_tolerates_rows_without_timestamps():
    """Callers pass sqlite3.Row in production and bare dicts in tests; a
    missing timestamp must degrade to "no duration", never raise."""
    task = {"name": "daily digest"}
    run = {"status": "succeeded", "summary": "all good", "error": "",
           "denied_actions": "[]"}
    assert notify.format_message(task, run) == "✅ daily digest\n\nall good"


def test_format_message_hides_a_nonsensical_duration():
    """started_at is 0 on a run that never left the queue."""
    task = {"name": "daily digest"}
    run = {"status": "succeeded", "summary": "s", "error": "",
           "denied_actions": "[]", "started_at": 0, "finished_at": 1042}
    assert notify.format_message(task, run) == "✅ daily digest\n\ns"
