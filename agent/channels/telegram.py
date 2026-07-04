# NimoOS-AI/agent/channels/telegram.py
"""Telegram channel adapter — thin httpx client over the official Bot API.
Long-polling (getUpdates) is deliberately chosen over webhooks: pure
outbound, works behind home NAT with no public ingress and no JWT-exempt
route. M1 scope: private chats, text messages only."""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile

import httpx

from channels.model import (ChannelAdapter, ChannelCapabilities,
                            InboundAttachment, InboundMessage, OutboundMessage)

_LOG = logging.getLogger("nimoos-agent.channels.telegram")

_API = "https://api.telegram.org/bot{token}/{method}"
_FILE_URL = "https://api.telegram.org/file/bot{token}/{file_path}"
_POLL_TIMEOUT = 50          # Telegram long-poll hold, seconds
_ERROR_BACKOFF = 5.0        # sleep after a failed poll round
_MAX_FILE = 20 * 1024 * 1024   # 20MB attachment download cap


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
            im = await self._to_inbound(upd)
            if im is None:
                continue
            # Dispatch without awaiting: a long agent run must not stall the
            # poll loop. Ordering per chat is enforced by the router's lock.
            t = asyncio.create_task(self._on_inbound(self, im))
            self._inflight.add(t)
            t.add_done_callback(self._inflight.discard)

    async def _to_inbound(self, upd: dict) -> InboundMessage | None:
        m = upd.get("message") or {}
        chat = m.get("chat") or {}
        frm = m.get("from") or {}
        # Telegram puts the caption of a photo/document in `caption`, not
        # `text` (which is only populated for pure-text messages). Fall back
        # to it so a captioned attachment doesn't silently drop the user's
        # instruction.
        text = m.get("text") or m.get("caption") or ""
        if chat.get("type") != "private":
            return None
        attachments = await self._extract_attachments(m)
        if not text and not attachments:
            return None
        return InboundMessage(
            channel_type="telegram", instance_id=self.instance_id,
            external_chat_id=str(chat.get("id", "")),
            external_user_id=str(frm.get("id", "")),
            external_username=frm.get("username"),
            message_id=str(m.get("message_id", "")), text=text, raw=upd,
            attachments=attachments)

    async def _extract_attachments(self, m: dict) -> list[InboundAttachment]:
        """Look for a photo (largest variant) or document on the message and
        download it. A single attachment's download failure is logged and
        skipped rather than failing the whole inbound message."""
        document = m.get("document")
        photo = m.get("photo")  # list of sizes, smallest first
        if document:
            file_id = document.get("file_id")
            filename = document.get("file_name") or f"{file_id}"
            mime = document.get("mime_type") or "application/octet-stream"
        elif photo:
            largest = photo[-1]
            file_id = largest.get("file_id")
            filename = f"{largest.get('file_unique_id', file_id)}.jpg"
            mime = "image/jpeg"
        else:
            return []
        if not file_id:
            return []
        try:
            tmp_path, size = await self._download_tg_file(file_id)
        except Exception as e:
            _LOG.warning("telegram attachment download failed (instance"
                         " %s, file_id %s): %s", self.instance_id, file_id, e)
            return []
        return [InboundAttachment(filename=filename, mime=mime,
                                  tmp_path=tmp_path, size=size)]

    async def _download_tg_file(self, file_id: str, *,
                                max_file: int = _MAX_FILE
                                ) -> tuple[str, int]:
        r = await self._client.get(self._url("getFile"),
                                   params={"file_id": file_id})
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"getFile not ok: {data}")
        file_path = data["result"]["file_path"]
        url = _FILE_URL.format(token=self._token, file_path=file_path)
        tmp = tempfile.NamedTemporaryFile(delete=False)
        total = 0
        try:
            async with self._client.stream("GET", url) as resp:
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_file:
                        raise ValueError(
                            f"attachment exceeds max_file ({max_file} bytes)")
                    tmp.write(chunk)
            tmp.close()
            return tmp.name, total
        except Exception:
            tmp.close()
            os.remove(tmp.name)
            raise

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
