"""Minimal 5-field cron parser (minute hour day-of-month month day-of-week).

Deliberately dependency-free: adding croniter would mean regenerating
requirements.lock and rebuilding the agent image, which this milestone
otherwise never needs. Supports `*`, `a`, `a-b`, `*/n`, `a-b/n` and
comma lists. No @aliases, no L/W/#. Times are interpreted in the box's
local timezone, which is what a NAS user means by "9am".
"""
from __future__ import annotations

import datetime as dt

_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]  # dow: 0 and 7 = Sunday
_HORIZON_DAYS = 366 * 4  # 覆盖闰年;超出即判定表达式永不触发


class CronError(ValueError):
    pass


def _parse_field(raw: str, idx: int) -> set[int]:
    lo, hi = _RANGES[idx]
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"empty field part in {raw!r}")
        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) < 1:
                raise CronError(f"bad step in {raw!r}")
            step = int(step_s)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise CronError(f"bad range in {raw!r}")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
        else:
            raise CronError(f"bad token {part!r}")
        if start < lo or end > hi or start > end:
            raise CronError(f"value out of range in {raw!r}")
        out.update(range(start, end + 1, step))
    if idx == 4 and 7 in out:
        out.discard(7)
        out.add(0)
    return out


def _fields(expr: str) -> list[set[int]]:
    parts = (expr or "").split()
    if len(parts) != 5:
        raise CronError("cron expression must have 5 fields")
    return [_parse_field(p, i) for i, p in enumerate(parts)]


def validate(expr: str) -> None:
    _fields(expr)


def next_after(expr: str, after_ts: int) -> int:
    """Smallest matching timestamp strictly greater than after_ts."""
    mins, hours, doms, months, dows = _fields(expr)
    raw = (expr or "").split()
    dom_restricted = raw[2] != "*"
    dow_restricted = raw[4] != "*"

    t = dt.datetime.fromtimestamp(after_ts).replace(second=0, microsecond=0)
    t += dt.timedelta(minutes=1)
    limit = t + dt.timedelta(days=_HORIZON_DAYS)
    while t < limit:
        if t.month not in months:
            # 跳到下个月 1 号 0:00
            t = (t.replace(day=1, hour=0, minute=0)
                 + dt.timedelta(days=32)).replace(day=1, hour=0, minute=0)
            continue
        dow = (t.weekday() + 1) % 7  # Python Mon=0 → cron Sun=0
        if dom_restricted and dow_restricted:
            day_ok = (t.day in doms) or (dow in dows)
        else:
            day_ok = (t.day in doms) and (dow in dows)
        if not day_ok:
            t = (t + dt.timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if t.hour not in hours:
            t = (t + dt.timedelta(hours=1)).replace(minute=0)
            continue
        if t.minute not in mins:
            t += dt.timedelta(minutes=1)
            continue
        return int(t.timestamp())
    raise CronError(f"expression never fires within horizon: {expr!r}")
