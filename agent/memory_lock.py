"""Process-wide per-user asyncio locks. Shared by the memory-extraction worker
and the remember() tool so a user's profile writes are mutually exclusive.
Hold ONLY around short DB critical sections — never across an LLM call."""
from __future__ import annotations

import asyncio

_locks: dict[str, asyncio.Lock] = {}


def get_user_lock(user_id) -> asyncio.Lock:
    key = str(user_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock
