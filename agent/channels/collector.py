"""Drain a RunSink subscription down to (final_text, error). Channels don't
stream in M1 — they wait for the terminal 'done' and reply once."""
from __future__ import annotations

import asyncio


async def collect_final(sink, timeout: float = 600.0) -> tuple[str, str | None]:
    final = ""
    error: str | None = None

    def apply(ev: dict) -> bool:
        nonlocal final, error
        t = ev.get("type")
        if t == "message":
            final = ev.get("content") or ""
        elif t == "error":
            error = error or (ev.get("content") or "agent error")
        return t == "done"

    past, q = sink.subscribe()
    try:
        for ev in past:
            if apply(ev):
                return final, error
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return final, error or "timeout"
            try:
                ev = await asyncio.wait_for(q.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return final, error or "timeout"
            if apply(ev):
                return final, error
    finally:
        sink.unsubscribe(q)
