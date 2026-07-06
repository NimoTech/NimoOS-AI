# NimoOS-AI/agent/channels/router.py
"""ChannelRouter — the platform-agnostic core. Resolves external identity
to a NimoOS user (deny-by-default pairing), maps chats to agent sessions,
handles commands, serializes per-chat execution, and relays the final
agent reply back through the adapter."""
from __future__ import annotations

import asyncio
import logging
import os
import time

from channels import store
from channels.collector import collect_final
from channels.driver import ChannelRunDriver
from channels.model import InboundMessage, OutboundMessage, split_text

_LOG = logging.getLogger("nimoos-agent.channels.router")

UNPAIRED_REPLY_INTERVAL = 600.0   # seconds between "not paired" hints
PAIR_FAIL_LIMIT = 5               # bad /pair attempts per window...
PAIR_FAIL_WINDOW = 3600.0         # ...per external user, then silence
MAX_PENDING = 3                   # queued messages per chat during a run

MAX_TRACKED_KEYS = 4096            # bound for stranger-keyed rate-limit dicts


def _prune(d: dict, cap: int | None = None) -> None:
    """Bound stranger-keyed dicts: when over cap, drop the oldest half
    (dict preserves insertion order). Protects against unique-id spraying."""
    limit = cap if cap is not None else MAX_TRACKED_KEYS
    if len(d) > limit:
        for k in list(d.keys())[: len(d) - limit // 2]:
            d.pop(k, None)

MSG_UNPAIRED = ("此账号尚未配对。请在 NimoOS 设置页(AI → Channels)生成配对码,"
                "然后发送 /pair <配对码>。(Not paired — send /pair <code>.)")
MSG_PAIR_USAGE = "用法: /pair <配对码> (usage: /pair <code>)"
MSG_PAIR_OK = "配对成功!现在可以直接发消息和你的 NimoOS AI 对话。(Paired.)"
MSG_PAIR_BAD = "配对码无效或已过期。(Invalid or expired code.)"
MSG_NO_MODEL = ("尚未为此渠道选择模型。请在 NimoOS 设置页(AI → Channels)"
                "为该绑定选择默认模型。(No default model configured.)")
MSG_CREDS_FAILED = ("无法解析该模型的凭据,请检查模型/供应商设置。"
                    "(Could not resolve provider credentials.)")
MSG_BUSY = "消息太多啦,请等当前回复完成后再发。(Too many pending messages.)"
MSG_NEW = "已开启新会话。(Started a new session.)"
MSG_STOP_OK = "已停止当前任务。(Stopped.)"
MSG_STOP_NONE = "当前没有正在运行的任务。(Nothing to stop.)"


class ChannelRouter:
    def __init__(self, conn, *, start_run, cancel_run, resolve_credentials,
                 run_timeout: float = 600.0):
        self._conn = conn
        self._start_run = start_run
        self._cancel_run = cancel_run
        self._resolve_credentials = resolve_credentials
        self._run_timeout = run_timeout
        self._chat_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._pending: dict[tuple[str, str], int] = {}
        self._unpaired_last: dict[tuple[str, str], float] = {}
        self._pair_fails: dict[tuple[str, str], list[float]] = {}

    # -- entry point ---------------------------------------------------------

    async def handle(self, adapter, msg: InboundMessage) -> None:
        try:
            await self._handle(adapter, msg)
        except Exception:
            _LOG.exception("channel message handling failed (instance %s)",
                           msg.instance_id)

    async def _handle(self, adapter, msg: InboundMessage) -> None:
        text = (msg.text or "").strip()
        if text.startswith("/pair"):
            await self._cmd_pair(adapter, msg, text)
            return
        binding = store.get_binding(self._conn, msg.instance_id,
                                    msg.external_user_id)
        if binding is None:
            await self._reply_unpaired(adapter, msg)
            return
        if text == "/whoami":
            await self._send_text(adapter, msg.external_chat_id,
                                  f"NimoOS user: {binding['user_id']}\n"
                                  f"model: {binding['default_model'] or '未设置 (not set)'}")
            return
        if text == "/new":
            await self._cmd_new(adapter, msg, binding)
            return
        if text == "/stop":
            await self._cmd_stop(adapter, msg)
            return
        await self._run_serialized(adapter, msg, binding)

    # -- commands ------------------------------------------------------------

    async def _cmd_pair(self, adapter, msg, text: str) -> None:
        parts = text.split()
        if len(parts) != 2:
            await self._send_text(adapter, msg.external_chat_id, MSG_PAIR_USAGE)
            return
        key = (msg.instance_id, msg.external_user_id)
        now = time.monotonic()
        fails = [t for t in self._pair_fails.get(key, [])
                 if now - t < PAIR_FAIL_WINDOW]
        binding = store.redeem_pairing_code(
            self._conn, msg.instance_id, parts[1], msg.external_user_id,
            msg.external_username, now_ms=int(time.time() * 1000))
        if binding is not None:
            self._pair_fails.pop(key, None)
            await self._send_text(adapter, msg.external_chat_id, MSG_PAIR_OK)
            return
        fails.append(now)
        self._pair_fails[key] = fails
        _prune(self._pair_fails)
        if len(fails) <= PAIR_FAIL_LIMIT:
            await self._send_text(adapter, msg.external_chat_id, MSG_PAIR_BAD)
        # beyond the limit: stay silent to starve brute-force probing

    async def _cmd_new(self, adapter, msg, binding) -> None:
        session_id = store.create_channel_session(
            self._conn, binding["user_id"], msg.channel_type)
        store.upsert_chat(self._conn, msg.instance_id, msg.external_chat_id,
                          binding["id"], session_id,
                          now_ms=int(time.time() * 1000))
        await self._send_text(adapter, msg.external_chat_id, MSG_NEW)

    async def _cmd_stop(self, adapter, msg) -> None:
        chat = store.get_chat(self._conn, msg.instance_id,
                              msg.external_chat_id)
        cancelled = False
        if chat is not None:
            cancelled = await self._cancel_run(chat["session_id"])
        await self._send_text(adapter, msg.external_chat_id,
                              MSG_STOP_OK if cancelled else MSG_STOP_NONE)

    # -- normal message ------------------------------------------------------

    async def _run_serialized(self, adapter, msg, binding) -> None:
        key = (msg.instance_id, msg.external_chat_id)
        if self._pending.get(key, 0) >= MAX_PENDING:
            await self._send_text(adapter, msg.external_chat_id, MSG_BUSY)
            return
        self._pending[key] = self._pending.get(key, 0) + 1
        lock = self._chat_locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:      # asyncio.Lock wakes waiters FIFO
                await self._process(adapter, msg, binding)
        finally:
            self._pending[key] -= 1

    async def _process(self, adapter, msg, binding) -> None:
        model = binding["default_model"]
        if not model:
            await self._send_text(adapter, msg.external_chat_id, MSG_NO_MODEL)
            return
        creds = await self._resolve_credentials(binding["user_id"], model)
        if not creds:
            await self._send_text(adapter, msg.external_chat_id,
                                  MSG_CREDS_FAILED)
            return
        chat = store.get_chat(self._conn, msg.instance_id,
                              msg.external_chat_id)
        if chat is None:
            session_id = store.create_channel_session(
                self._conn, binding["user_id"], msg.channel_type)
            store.upsert_chat(self._conn, msg.instance_id,
                              msg.external_chat_id, binding["id"], session_id,
                              now_ms=int(time.time() * 1000))
        else:
            session_id = chat["session_id"]
        await adapter.send_typing(msg.external_chat_id)
        attachment_ids: list[str] = []
        if msg.attachments:
            from channels import inbound
            import db as _db
            ddir = binding.get("download_dir") or f"/DATA/Downloads/{msg.channel_type}"
            data_root = os.environ.get(
                "NIMOOS_AGENT_DATA_ROOT", str(_db._DB_PATH.parent))
            attachment_ids, skipped = inbound.save_and_ingest(
                self._conn, data_root, session_id, ddir, msg.attachments)
            if skipped:
                await self._send_text(adapter, msg.external_chat_id,
                                      f"部分文件超出限制已跳过 (skipped): {', '.join(skipped)}")
        run_text = msg.text
        if not run_text and not attachment_ids:
            # Nothing to run: no text, and no attachment was actually
            # ingested (either none were sent, or all were skipped — the
            # user already got the skip notice above).
            return
        if not run_text and attachment_ids:
            run_text = ("[用户发来文件/图片,已存至 " + (binding.get("download_dir")
                        or f"/DATA/Downloads/{msg.channel_type}")
                        + "。请分析,或询问希望如何处理。]")
        send_cb = None
        if getattr(adapter.capabilities, "supports_media", False):
            async def send_cb(path, caption, _a=adapter, _c=msg.external_chat_id):
                mid = await _a.send_file(_c, path, caption)
                if mid is None:
                    raise RuntimeError("send_file returned no id")
                return mid
        sink = self._start_run(session_id, binding["user_id"], run_text,
                               creds, binding.get("external_username") or "",
                               attachment_ids=attachment_ids,
                               channel_send_file=send_cb)

        async def _send(text, _a=adapter, _c=msg.external_chat_id):
            await self._send_text(_a, _c, text)

        driver = ChannelRunDriver(send_text=_send,
                                  surface_confirm=None,   # wired in Task B4
                                  run_timeout=self._run_timeout)
        await driver.drive(sink)

    # -- helpers ---------------------------------------------------------------

    async def _reply_unpaired(self, adapter, msg) -> None:
        key = (msg.instance_id, msg.external_user_id)
        now = time.monotonic()
        last = self._unpaired_last.get(key)
        if last is not None and now - last < UNPAIRED_REPLY_INTERVAL:
            return
        self._unpaired_last[key] = now
        _prune(self._unpaired_last)
        await self._send_text(adapter, msg.external_chat_id, MSG_UNPAIRED)

    async def _send_text(self, adapter, chat_id: str, text: str) -> None:
        for chunk in split_text(text, adapter.capabilities.max_text_len):
            await adapter.send(chat_id, OutboundMessage(text=chunk))
