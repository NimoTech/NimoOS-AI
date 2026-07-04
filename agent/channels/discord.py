"""Discord channel adapter — official Bot API over the Gateway WebSocket
(discord.py). Parity with the Telegram M1 adapter: DM-only, plain text.
discord.py is imported lazily (imported lazily inside methods, never at
module scope) so message-mapping / validate-token / injected-client tests
need not install it.
Bots may only DM users who share a server with them; pairing therefore
requires the user to join a server the bot is in (see spec)."""
from __future__ import annotations

import asyncio
import logging

import httpx

from channels.model import (ChannelAdapter, ChannelCapabilities,
                            InboundMessage, OutboundMessage)

_LOG = logging.getLogger("nimoos-agent.channels.discord")

_API = "https://discord.com/api/v10"


class DiscordAdapter(ChannelAdapter):
    channel_type = "discord"
    capabilities = ChannelCapabilities(max_text_len=2000, supports_typing=True)

    def __init__(self, instance_id, config, on_inbound, *,
                 client=None):
        super().__init__(instance_id, config, on_inbound)
        self._token = config["bot_token"]
        self._client = client            # injected in tests; else built in start()
        self._task: asyncio.Task | None = None
        self._inflight: set[asyncio.Task] = set()

    # -- message shape helpers (duck-typed; no discord import) ----------------

    def _is_dm(self, message) -> bool:
        import discord
        return isinstance(message.channel, discord.DMChannel)

    def _is_self(self, message) -> bool:
        me = getattr(self._client, "user", None)
        return me is not None and message.author.id == me.id

    def _to_inbound(self, message) -> InboundMessage | None:
        if self._is_self(message):
            return None
        if not self._is_dm(message):
            return None
        text = message.content or ""
        if not text:
            return None
        return InboundMessage(
            channel_type="discord", instance_id=self.instance_id,
            external_chat_id=str(message.channel.id),
            external_user_id=str(message.author.id),
            external_username=getattr(message.author, "name", None),
            message_id=str(message.id), text=text, raw={})

    async def _handle_message(self, message) -> None:
        im = self._to_inbound(message)
        if im is None:
            return
        # Fire-and-forget: a long agent run must not stall the gateway loop.
        t = asyncio.create_task(self._on_inbound(self, im))
        self._inflight.add(t)
        t.add_done_callback(self._inflight.discard)

    # -- lifecycle ------------------------------------------------------------

    def _build_client(self):
        import discord
        intents = discord.Intents.none()
        intents.dm_messages = True
        intents.message_content = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_message(message):   # noqa: unused — registered by decorator
            try:
                await self._handle_message(message)
            except Exception:
                _LOG.exception("discord message handling failed (instance %s)",
                               self.instance_id)
        return client

    async def start(self) -> None:
        if self._client is None:
            self._client = self._build_client()
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            await self._client.start(self._token)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _LOG.error("discord client stopped (instance %s): %s — if this is "
                       "PrivilegedIntentsRequired, enable the Message Content "
                       "Intent for the bot in the Developer Portal",
                       self.instance_id, e)

    async def stop(self) -> None:
        try:
            await self._client.close()
        except Exception:
            pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def send(self, external_chat_id: str,
                   msg: OutboundMessage) -> str | None:
        cid = int(external_chat_id)
        channel = self._client.get_channel(cid)
        if channel is None:
            channel = await self._client.fetch_channel(cid)
        sent = await channel.send(msg.text)
        return str(getattr(sent, "id", "")) or None

    async def send_typing(self, external_chat_id: str) -> None:
        try:
            cid = int(external_chat_id)
            channel = self._client.get_channel(cid)
            if channel is None:
                channel = await self._client.fetch_channel(cid)
            await channel.typing()
        except Exception:
            pass

    @staticmethod
    async def validate_token(token: str, *,
                             transport: httpx.AsyncBaseTransport | None = None
                             ) -> dict | None:
        try:
            async with httpx.AsyncClient(transport=transport, timeout=10) as client:
                r = await client.get(_API + "/users/@me",
                                     headers={"Authorization": "Bot " + token})
            data = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        if r.status_code != 200 or not isinstance(data, dict) or not data.get("id"):
            return None
        return {"bot_username": data.get("username", ""),
                "application_id": str(data["id"])}
