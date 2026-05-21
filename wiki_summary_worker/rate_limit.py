"""Worker-side rolling-window rate limiter.

Persists call timestamps to ~/.cache/nimoos-wiki-summary/calls.log so the
limit holds across worker invocations (systemd Type=oneshot relaunches the
worker every 5min). Single-instance assumption: systemd unit ensures no
two copies run concurrently — no file lock needed.
"""
from __future__ import annotations
import time
from pathlib import Path


class RateLimitExceeded(Exception):
    pass


class RateLimiter:
    PATH = Path("~/.cache/nimoos-wiki-summary/calls.log").expanduser()

    def take_or_die(self, max_per_hour: int) -> None:
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - 3_600_000

        self.PATH.parent.mkdir(parents=True, exist_ok=True)

        existing: list[int] = []
        if self.PATH.exists():
            for line in self.PATH.read_text().splitlines():
                try:
                    ts = int(line.strip())
                except ValueError:
                    continue
                if ts > cutoff:
                    existing.append(ts)

        if len(existing) >= max_per_hour:
            age = (now_ms - existing[0]) / 1000.0
            raise RateLimitExceeded(
                f"hit {max_per_hour}/h cap (oldest call {age:.0f}s ago)"
            )

        existing.append(now_ms)
        self.PATH.write_text("\n".join(str(t) for t in existing))
