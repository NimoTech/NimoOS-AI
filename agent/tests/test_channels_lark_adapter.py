# NimoOS-AI/agent/tests/test_channels_lark_adapter.py
"""M4 第一段:LarkAdapter 的 channel 语义。

进程层已在 test_channels_lark_cli.py 覆盖,这里替换掉 `run_once` / `Consumer`,
只断言适配器怎么用它们 —— 尤其是两件容易做错的事:

* **身份必须是 bot。** 用 user 身份发,任务失败摘要看起来像用户自己发的,而且
  user token 会过期,无人值守场景不能依赖。
* **飞书卡片的 action_value 是开发者自定义 JSON,不是 Telegram 那种不透明
  字符串。** 上层给的 callback_data 必须能原样取回,否则确认回调认不出
  confirm_id。
"""
import asyncio
import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

from channels import lark as lark_channel
from channels import lark_cli
from channels.model import OutboundMessage


class _FakeConsumer:
    instances = []

    def __init__(self, uid, event_key, on_event, *, on_state=None, **kw):
        self.uid, self.event_key = uid, event_key
        self.on_event, self.on_state = on_event, on_state
        self.started = self.stopped = False
        self.ready = False
        _FakeConsumer.instances.append(self)

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


@pytest.fixture
def calls(monkeypatch):
    """Capture run_once invocations; return (rc, stdout, stderr) per script."""
    recorded = []
    script = {"rc": 0, "out": '{"ok": true, "data": {"message_id": "om_1"}}', "err": ""}

    async def _fake_run_once(uid, args, *, timeout=20.0):
        recorded.append({"uid": uid, "args": list(args), "timeout": timeout})
        return script["rc"], script["out"], script["err"]

    monkeypatch.setattr(lark_channel.lark_cli, "run_once", _fake_run_once)
    _FakeConsumer.instances = []
    monkeypatch.setattr(lark_channel.lark_cli, "Consumer", _FakeConsumer)
    return recorded, script


def _adapter(on_callback=None):
    async def _noop_inbound(adapter, msg):
        return None
    return lark_channel.LarkAdapter(
        "inst-1", {"uid": "1"}, _noop_inbound, on_callback=on_callback)


@pytest.mark.asyncio
async def test_send_uses_bot_identity_and_the_configured_uid(calls):
    recorded, _ = calls
    a = _adapter()
    ok = await a.send("ou_abc", OutboundMessage(text="hello"))
    assert ok is not False
    assert len(recorded) == 1
    args = recorded[0]["args"]
    assert recorded[0]["uid"] == "1"
    assert "--as" in args and args[args.index("--as") + 1] == "bot"
    assert "hello" in args


@pytest.mark.asyncio
async def test_send_carries_an_idempotency_key(calls):
    recorded, _ = calls
    a = _adapter()
    await a.send("ou_abc", OutboundMessage(text="hello"))
    assert "--idempotency-key" in recorded[0]["args"]


@pytest.mark.asyncio
async def test_send_addresses_open_ids_as_user_and_chat_ids_as_chat(calls):
    recorded, _ = calls
    a = _adapter()
    await a.send("ou_abc", OutboundMessage(text="x"))
    await a.send("oc_xyz", OutboundMessage(text="x"))
    assert "--user-id" in recorded[0]["args"]
    assert "--chat-id" in recorded[1]["args"]


@pytest.mark.asyncio
async def test_send_failure_returns_falsey_and_does_not_raise(calls):
    recorded, script = calls
    script.update(rc=1, out="", err="boom")
    a = _adapter()
    assert not await a.send("ou_abc", OutboundMessage(text="x"))


@pytest.mark.asyncio
async def test_missing_cli_does_not_raise(calls):
    recorded, script = calls
    script.update(rc=lark_cli.RC_NO_CLI, out="", err="lark-cli not found")
    a = _adapter()
    assert not await a.send("ou_abc", OutboundMessage(text="x"))


def _recorder():
    """`router.handle_confirm` is a COROUTINE function, so the fake must be one
    too — a sync stub would make the adapter look correct while hiding that it
    forgot to schedule the real thing."""
    got = []

    async def _cb(adapter, chat_id, callback_data):
        got.append((adapter, chat_id, callback_data))

    return got, _cb


@pytest.mark.asyncio
async def test_send_buttons_round_trips_callback_data_through_the_card(calls):
    recorded, _ = calls
    got, cb = _recorder()
    a = _adapter(on_callback=cb)
    await a.start()

    mid = await a.send_buttons("ou_abc", "Confirm?",
                               [("Allow", "cf:c1:a"), ("Deny", "cf:c1:d")])
    assert mid == "om_1"

    args = recorded[-1]["args"]
    assert args[args.index("--msg-type") + 1] == "interactive"
    card = json.loads(args[args.index("--content") + 1])
    blob = json.dumps(card)
    assert "cf:c1:a" in blob and "cf:c1:d" in blob
    # The address must ride along: the click event reports the conversation id,
    # not the open_id a DM was addressed by.
    assert "ou_abc" in blob


@pytest.mark.asyncio
async def test_a_card_click_hands_back_the_callback_data_and_the_sent_address(calls):
    got, cb = _recorder()
    a = _adapter(on_callback=cb)
    await a.start()
    consumer = _FakeConsumer.instances[-1]

    consumer.on_event({
        "action_value": json.dumps({"cd": "cf:c1:a", "to": "ou_abc"}),
        "action_tag": "button",
        # Feishu reports the CONVERSATION here, which is not what we sent to.
        "open_chat_id": "oc_zzz",
    })
    await asyncio.sleep(0)          # let the scheduled callback task run

    assert len(got) == 1
    _adapter_arg, chat_id, callback_data = got[0]
    assert callback_data == "cf:c1:a"
    assert chat_id == "ou_abc", (
        "handle_confirm rejects an ownership mismatch, so the click must come "
        "back addressed the way the card was sent")


@pytest.mark.asyncio
async def test_a_click_missing_either_half_is_ignored(calls):
    got, cb = _recorder()
    a = _adapter(on_callback=cb)
    await a.start()
    consumer = _FakeConsumer.instances[-1]

    for ev in ({"action_value": "not json"},
               {"action_value": "{}"},
               {"action_value": json.dumps({"cd": "cf:c1:a"})},   # no address
               {"action_value": json.dumps({"to": "ou_abc"})},    # no data
               {}):
        consumer.on_event(ev)
    await asyncio.sleep(0)

    assert got == []


@pytest.mark.asyncio
async def test_buttons_are_unsupported_until_the_consumer_is_ready(calls):
    """Sending a card nobody is listening for would strand the run waiting."""
    _got, cb = _recorder()
    a = _adapter(on_callback=cb)
    await a.start()
    consumer = _FakeConsumer.instances[-1]

    assert a.buttons_available is False
    consumer.ready = True
    consumer.on_state(True)
    assert a.buttons_available is True


@pytest.mark.asyncio
async def test_start_and_stop_drive_the_consumer(calls):
    _got, cb = _recorder()
    a = _adapter(on_callback=cb)
    await a.start()
    consumer = _FakeConsumer.instances[-1]
    assert consumer.started is True
    assert consumer.event_key == "card.action.trigger"
    await a.stop()
    assert consumer.stopped is True


@pytest.mark.asyncio
async def test_no_callback_means_no_long_connection(calls):
    """Notification-only wiring must not hold a Feishu long connection open
    for clicks nobody would consume."""
    a = _adapter()                      # no on_callback
    await a.start()
    assert _FakeConsumer.instances == []
    assert a.buttons_available is False
    await a.stop()                      # must not raise with no consumer


def test_registered_in_the_adapter_registry():
    from channels.manager import ADAPTERS
    assert ADAPTERS["lark"] is lark_channel.LarkAdapter
