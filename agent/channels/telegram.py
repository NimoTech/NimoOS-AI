# NimoOS-AI/agent/channels/telegram.py
"""Telegram channel adapter — thin httpx client over the official Bot API.
Long-polling (getUpdates) is deliberately chosen over webhooks: pure
outbound, works behind home NAT with no public ingress and no JWT-exempt
route. M1 scope: private chats, text messages only."""
from __future__ import annotations

import asyncio
import logging

import httpx

from channels.model import (ChannelAdapter, ChannelCapabilities,
                            InboundMessage, OutboundMessage)

_LOG = logging.getLogger("nimoos-agent.channels.telegram")

_API = "https://api.telegram.org/bot{token}/{method}"
_POLL_TIMEOUT = 50          # Telegram long-poll hold, seconds
_ERROR_BACKOFF = 5.0        # sleep after a failed poll round


class TelegramAdapter(ChannelAdapter):
    channel_type = "telegram"
    capabilities = ChannelCapabilities(max_text_len=4096, supports_typing=True)

    def __init__(self, instance_id, config, on_inbound, *,
                 transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(instance_id, config, on_inbound)
        self._token = config["bot_token"]
        self._client = httpx.AsyncClient(transport=transport,
                                         timeout=_POLL_TIMEOUT + 15)
        self._offset = 0
        self._task: asyncio.Task | None = None
        self._inflight: set[asyncio.Task] = set()

    def _url(self, method: str) -> str:
        return _API.format(token=self._token, method=method)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._client.aclose()

    async def _run_loop(self) -> None:
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _LOG.warning("telegram poll error (instance %s): %s",
                             self.instance_id, e)
                await asyncio.sleep(_ERROR_BACKOFF)

    async def _poll_once(self) -> None:
        r = await self._client.get(self._url("getUpdates"), params={
            "offset": self._offset, "timeout": _POLL_TIMEOUT,
            "allowed_updates": '["message"]'})
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"getUpdates not ok: {data}")
        for upd in data.get("result", []):
            self._offset = max(self._offset, int(upd["update_id"]) + 1)
            im = self._to_inbound(upd)
            if im is None:
                continue
            # Dispatch without awaiting: a long agent run must not stall the
            # poll loop. Ordering per chat is enforced by the router's lock.
            t = asyncio.create_task(self._on_inbound(self, im))
            self._inflight.add(t)
            t.add_done_callback(self._inflight.discard)

    def _to_inbound(self, upd: dict) -> InboundMessage | None:
        m = upd.get("message") or {}
        chat = m.get("chat") or {}
        frm = m.get("from") or {}
        text = m.get("text")
        if chat.get("type") != "private" or not text:
            return None
        return InboundMessage(
            channel_type="telegram", instance_id=self.instance_id,
            external_chat_id=str(chat.get("id", "")),
            external_user_id=str(frm.get("id", "")),
            external_username=frm.get("username"),
            message_id=str(m.get("message_id", "")), text=text, raw=upd)

    async def send(self, external_chat_id: str,
                   msg: OutboundMessage) -> str | None:
        r = await self._client.post(self._url("sendMessage"), json={
            "chat_id": external_chat_id, "text": msg.text})
        data = r.json()
        if not data.get("ok"):
            _LOG.warning("sendMessage failed (instance %s): %s",
                         self.instance_id, data)
            return None
        return str(data["result"]["message_id"])

    async def send_typing(self, external_chat_id: str) -> None:
        try:
            await self._client.post(self._url("sendChatAction"), json={
                "chat_id": external_chat_id, "action": "typing"})
        except httpx.HTTPError:
            pass

    @staticmethod
    async def validate_token(token: str, *,
                             transport: httpx.AsyncBaseTransport | None = None
                             ) -> dict | None:
        try:
            async with httpx.AsyncClient(transport=transport,
                                         timeout=10) as client:
                r = await client.get(_API.format(token=token, method="getMe"))
            data = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        if r.status_code != 200 or not data.get("ok"):
            return None
        return {"bot_username": data["result"].get("username", "")}
