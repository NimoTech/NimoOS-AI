"""Drain a RunSink subscription down to (final_text, error). Channels don't
stream in M1 — they wait for the terminal 'done' and reply once."""
from __future__ import annotations

import asyncio


async def collect_final(sink, timeout: float = 600.0) -> tuple[str, str | None]:
    # A run's reply reaches the sink one of two ways:
    #   - streaming models emit a series of 'message_delta' chunks and SUPPRESS
    #     the terminal 'message' event (agent.py: `if streamed_message: continue`),
    #   - non-streaming / reasoning-only fallback emits a single 'message' event.
    # We must handle both: accumulate deltas, and prefer a full 'message' event
    # when one arrives. (Ignoring deltas made every streamed reply come back
    # empty — the "(no reply)" bug.)
    final = ""        # authoritative full text from a terminal 'message' event
    delta_buf = ""    # accumulated streaming 'message_delta' chunks
    error: str | None = None

    def apply(ev: dict) -> bool:
        nonlocal final, delta_buf, error
        t = ev.get("type")
        if t == "message":
            final = ev.get("content") or ""
        elif t == "message_delta":
            delta_buf += ev.get("content") or ""
        elif t == "error":
            error = error or (ev.get("content") or "agent error")
        return t == "done"

    def result(err: str | None) -> tuple[str, str | None]:
        return (final or delta_buf), err

    past, q = sink.subscribe()
    try:
        for ev in past:
            if apply(ev):
                return result(error)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return result(error or "timeout")
            try:
                ev = await asyncio.wait_for(q.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return result(error or "timeout")
            if apply(ev):
                return result(error)
    finally:
        sink.unsubscribe(q)
