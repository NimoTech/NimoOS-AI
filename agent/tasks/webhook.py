"""M3: rate limiting for the webhook trigger — pure, no I/O.

The webhook endpoint has no JWT: the task's `webhook_token` is the whole
credential.  Guessing one is not the threat (128 bits of entropy); the threat
is a *valid* token called in a loop, because every call writes a `task_runs`
row even when `overlap_policy` later marks the run `skipped`.  So the limit is
per task, not per caller: it bounds how fast one task can be asked to run,
which is the resource that actually gets consumed.

Deliberately in-memory and per-process.  A restart forgetting that a task was
triggered 3 seconds ago costs one extra queued run, which the overlap policy
already handles — not worth a table.
"""
from __future__ import annotations

import time

# Minimum gap between two accepted triggers of the SAME task. The scheduler
# ticks every 15s and a run typically outlives that, so anything finer would
# only queue runs that overlap-skip anyway.
WINDOW_SECONDS = 10


class RateLimiter:
    """Least-recent-use-free fixed-window limiter keyed by task id."""

    def __init__(self, window_seconds: int = WINDOW_SECONDS):
        self._window = window_seconds
        self._last: dict[str, float] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        """True if `key` may fire now; records the hit when it returns True."""
        now = time.monotonic() if now is None else now
        # Prune first so a long-lived process cannot accumulate an entry per
        # task id ever seen (task ids are uuids — deleted tasks never return).
        self._last = {k: t for k, t in self._last.items()
                      if now - t < self._window}
        last = self._last.get(key)
        if last is not None and now - last < self._window:
            return False
        self._last[key] = now
        return True

    def reset(self) -> None:
        """Drop all state. For tests — never call this from a request path."""
        self._last.clear()


# One limiter for the process. The endpoint imports this rather than building
# its own, so the window is shared across every worker coroutine.
RATE_LIMITER = RateLimiter()
