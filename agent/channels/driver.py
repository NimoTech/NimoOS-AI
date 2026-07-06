"""ChannelRunDriver — consume a RunSink live and drive the chat side of a run.

Two jobs:
  1. Progress push: accumulate streamed `message_delta` chunks; flush the
     buffer as one "conclusion" message at each tool-call boundary and at the
     terminal `done`. (Replaces collect_final on the channel hot path.)
  2. Interactive confirms: on `access_request` / `confirmation_required`,
     flush any pending conclusion first, then hand the raw event to a
     router-provided `surface_confirm` (which renders buttons + owns timeout).

The driver never touches the DB or resolves confirms itself — it only
observes the stream and forwards. This keeps it unit-testable with a fake sink.
"""
from __future__ import annotations

import asyncio
import time

_CONFIRM_TYPES = ("access_request", "confirmation_required")


class ChannelRunDriver:
    def __init__(self, *, send_text, surface_confirm=None,
                 run_timeout: float = 600.0, min_interval: float = 1.0,
                 sleep=asyncio.sleep, now=time.monotonic):
        self._send_text = send_text              # async (text) -> None
        self._surface_confirm = surface_confirm  # async (ev: dict) -> None | None
        self._run_timeout = run_timeout
        self._min_interval = min_interval
        self._sleep = sleep
        self._now = now
        self._last_send = 0.0
        self._buf = ""
        self._sent_anything = False
        self._error: str | None = None

    async def _emit(self, text: str) -> None:
        if not text:
            return
        if self._last_send:
            gap = self._now() - self._last_send
            if gap < self._min_interval:
                await self._sleep(self._min_interval - gap)
        await self._send_text(text)
        self._last_send = self._now()
        self._sent_anything = True

    async def _flush(self) -> None:
        text = self._buf
        self._buf = ""
        if text.strip():
            await self._emit(text)

    async def _apply(self, ev: dict) -> bool:
        t = ev.get("type")
        if t == "message_delta":
            self._buf += ev.get("content") or ""
        elif t == "message":
            self._buf = ev.get("content") or ""
        elif t in ("tool_call", "function_call"):
            await self._flush()
        elif t in _CONFIRM_TYPES:
            await self._flush()                  # conclusion before the ask
            if self._surface_confirm is not None and ev.get("confirm_id"):
                await self._surface_confirm(ev)
        elif t == "error":
            self._error = ev.get("content") or "agent error"
        return t == "done"

    async def _finish(self) -> None:
        await self._flush()
        if self._error:
            await self._emit(f"出错了 (error): {self._error}")
        elif not self._sent_anything:
            await self._emit("(无回复 / empty reply)")

    async def drive(self, sink) -> None:
        past, q = sink.subscribe()
        try:
            for ev in past:
                if await self._apply(ev):
                    await self._finish()
                    return
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._run_timeout
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    self._error = self._error or "timeout"
                    await self._finish()
                    return
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    self._error = self._error or "timeout"
                    await self._finish()
                    return
                if await self._apply(ev):
                    await self._finish()
                    return
        finally:
            sink.unsubscribe(q)
