"""M4:飞书 channel 适配器(通知 + 确认卡传输)。

只做两件事:出站发消息/发卡片,入站把卡片点击还给上层。**不消费
`im.message.receive_v1`** —— 本期不做"在飞书里与 agent 对话"(spec §0),所以
`on_inbound` 永不触发。

翻译责任在这里落地:上层的 callback_data 是一个不透明字符串(Telegram 的形
状),而飞书卡片的 `action_value` 是开发者自定义的 JSON 字符串。适配器把字符串
包进 `{"cd": ...}` 再取回,两个 channel 的上层代码因此保持一致。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid

from channels import lark_cli
from channels.model import ChannelAdapter, ChannelCapabilities, OutboundMessage

_LOG = logging.getLogger("nimoos-agent.channels.lark")

DEFAULT_EVENT_KEY = "card.action.trigger"

# Feishu's own limit is larger, but the router splits on this and a shorter
# chunk reads better on mobile.
MAX_TEXT_LEN = 4000

SEND_TIMEOUT = 20.0


def _is_open_id(external_chat_id: str) -> bool:
    """`ou_...` is a person (send as a DM); `oc_...` is a chat.

    A notification target built from the Feishu binding is an open_id, because
    a bot cannot list its own p2p chats (`im +chat-list --types=p2p` is
    user-only), so there is no chat id to store for a DM.
    """
    return str(external_chat_id).startswith("ou_")


class LarkAdapter(ChannelAdapter):
    channel_type = "lark"
    capabilities = ChannelCapabilities(
        max_text_len=MAX_TEXT_LEN,
        supports_edit=True,
        supports_buttons=True,
        supports_typing=False,
        supports_media=False,
    )

    def __init__(self, instance_id, config, on_inbound, on_callback=None):
        super().__init__(instance_id, config, on_inbound, on_callback)
        self._uid = str(config.get("uid") or "")
        self._event_key = config.get("event_key") or DEFAULT_EVENT_KEY
        self._consumer = None
        self._ready = False
        # Strong references to in-flight callback tasks; without this the
        # event loop may collect one mid-await and the click is lost.
        self._inflight: set = set()

    # -- lifecycle ---------------------------------------------------------

    @property
    def buttons_available(self) -> bool:
        """Buttons work only while the click consumer is up.

        `capabilities` is a class-level declaration; this is the runtime truth.
        Callers that are about to strand a run on a confirmation must consult
        it — a card whose click nobody receives is worse than an immediate deny.
        """
        return self._ready

    async def start(self) -> None:
        if self._on_callback is None:
            # Notification-only wiring: nothing would consume a click, so do
            # not hold a long connection open for it.
            _LOG.info("lark channel %s started without a callback handler; "
                      "click consumer not launched", self.instance_id)
            return
        self._consumer = lark_cli.Consumer(
            self._uid, self._event_key, self._on_card_event,
            on_state=self._on_consumer_state)
        await self._consumer.start()

    async def stop(self) -> None:
        consumer, self._consumer = self._consumer, None
        self._ready = False
        if consumer is not None:
            await consumer.stop()

    def _on_consumer_state(self, ready: bool) -> None:
        self._ready = bool(ready)

    # -- inbound (clicks only) --------------------------------------------

    def _on_card_event(self, ev: dict) -> None:
        """Called from the consumer's (synchronous) line loop.

        Two things here are easy to get wrong:

        * `on_callback` is a COROUTINE function (`router.handle_confirm`), so it
          must be scheduled, not called — calling it would leave an un-awaited
          coroutine and silently drop every click. Mirrors `telegram.py`'s
          `_dispatch_callback`, including keeping a reference to the task so it
          is not garbage-collected mid-flight.
        * The click event's chat id is NOT the address we sent to. A DM is
          addressed by the user's `ou_` open_id (a bot cannot list its own p2p
          chats), while the event reports the `oc_` conversation. `handle_confirm`
          rejects a mismatch as an ownership failure, so the address rides in the
          card itself and comes back verbatim.
        """
        raw = ev.get("action_value")
        if not isinstance(raw, str) or not raw:
            return
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            _LOG.warning("lark: card action_value was not JSON; ignoring")
            return
        if not isinstance(payload, dict):
            return
        callback_data = payload.get("cd")
        target = payload.get("to")
        if not isinstance(callback_data, str) or not callback_data:
            return
        if not isinstance(target, str) or not target:
            _LOG.warning("lark: card click carried no target address; ignoring")
            return
        if self._on_callback is None:
            return
        task = asyncio.create_task(
            self._on_callback(self, target, callback_data))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    # -- outbound ----------------------------------------------------------

    def _target_args(self, external_chat_id: str) -> list[str]:
        flag = "--user-id" if _is_open_id(external_chat_id) else "--chat-id"
        return [flag, str(external_chat_id)]

    async def _send_args(self, args: list[str]) -> str | None:
        """Run one send.

        Returns the message id; `""` when the send succeeded but the id could
        not be parsed (the message reached the user, so this is NOT a failure —
        only edit-based features degrade); `None` when the send failed.
        """
        rc, out, err = await lark_cli.run_once(
            self._uid, args, timeout=SEND_TIMEOUT)
        if rc != 0:
            _LOG.warning("lark send failed rc=%s: %s", rc, (err or out)[:300])
            return None
        try:
            doc = json.loads(out)
        except (ValueError, TypeError):
            _LOG.warning("lark send: unparsable CLI output")
            return None
        if not isinstance(doc, dict) or doc.get("ok") is False:
            _LOG.warning("lark send: CLI reported failure")
            return None
        data = doc.get("data")
        if isinstance(data, dict):
            mid = data.get("message_id") or data.get("message_id_str")
            if isinstance(mid, str) and mid:
                return mid
        # Sent, but the id is not where we expected it. Report success — the
        # user got the message — and let edit-based features degrade.
        return ""

    async def send(self, external_chat_id: str,
                   message: OutboundMessage) -> str | None:
        """Returns the message id; `""` when the send succeeded but the id
        could not be parsed (the message reached the user, so this is NOT a
        failure — only edit-based features degrade); `None` when the send
        failed."""
        args = ["im", "+messages-send", "--as", "bot",
                *self._target_args(external_chat_id),
                "--text", message.text,
                "--idempotency-key", uuid.uuid4().hex[:32]]
        return await self._send_args(args)

    async def send_buttons(self, external_chat_id: str, text: str,
                           buttons: "list[tuple[str, str]]") -> str | None:
        """Returns the message id; `""` when the send succeeded but the id
        could not be parsed (the message reached the user, so this is NOT a
        failure — only edit-based features degrade); `None` when the send
        failed."""
        card = _build_card(text, buttons, target=str(external_chat_id))
        args = ["im", "+messages-send", "--as", "bot",
                *self._target_args(external_chat_id),
                "--msg-type", "interactive",
                "--content", json.dumps(card, ensure_ascii=False),
                "--idempotency-key", uuid.uuid4().hex[:32]]
        return await self._send_args(args)

    async def edit_to_resolved(self, external_chat_id: str, message_id: str,
                               text: str) -> None:
        """Best-effort: replace the card with plain resolved text.

        The callback token lives 30 minutes and allows at most 2 updates, so a
        late edit legitimately fails; when it does, send a follow-up message
        instead of leaving the user with a card whose buttons do nothing.
        """
        if not message_id:
            # Sent, but the id never parsed — there is nothing to update, so go
            # straight to the follow-up rather than spending a failed API call.
            await self.send(external_chat_id, OutboundMessage(text=text))
            return
        card = _build_card(text, [])
        rc, _out, err = await lark_cli.run_once(
            self._uid,
            ["api", "POST", "/open-apis/interactive/v1/card/update",
             "--as", "bot",
             "--data", json.dumps({"card": card}, ensure_ascii=False)],
            timeout=SEND_TIMEOUT)
        if rc != 0:
            _LOG.info("lark: card update failed (%s); sending a follow-up",
                      (err or "")[:200])
            await self.send(external_chat_id, OutboundMessage(text=text))


def _build_card(text: str, buttons: "list[tuple[str, str]]",
                target: str = "") -> dict:
    """A minimal interactive card.

    Each button carries the upper layer's opaque `callback_data` in `value.cd`
    AND the address the card was sent to in `value.to`. The address has to make
    the round trip because the click event reports the conversation id, not the
    open_id a DM was addressed by, and `handle_confirm` treats a mismatch as an
    ownership failure.
    """
    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "plain_text", "content": text}},
    ]
    if buttons:
        elements.append({
            "tag": "action",
            "actions": [
                {"tag": "button",
                 "text": {"tag": "plain_text", "content": label},
                 "type": "default",
                 "value": {"cd": callback_data, "to": target}}
                for label, callback_data in buttons
            ],
        })
    return {"config": {"wide_screen_mode": True}, "elements": elements}
