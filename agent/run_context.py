"""Per-run mutable state for mid-run compaction (spec §5.1).

One RunCtx per agent run, published through RUN_CTX_VAR by agent.py before
Runner.run_streamed and read by compaction_filter (before every model call)
and ContextHooks (after every model call). asyncio tasks copy the context, so
parallel tool calls see the same object; only the run loop writes to it.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RunCtx:
    session_id: str
    user_id: str
    model_name: str
    provider_type: str
    window: int
    conn: Any = None
    summarize_fn: Any = None
    overhead_tokens: int = 0
    last_input_tokens: int = 0
    peak_input_tokens: int = 0
    items_seen_at_last_call: int = 0
    summary: str = ""
    fold_idx: int = 0
    persist_prefix_len: int = 0
    l1_count: int = 0
    l1_reasoning_count: int = 0
    l2_count: int = 0
    l2_fail_count: int = 0
    l2_disabled: bool = False
    trunc_count: int = 0
    compaction_enabled: bool = True
    extra: dict = field(default_factory=dict)


RUN_CTX_VAR: ContextVar["RunCtx | None"] = ContextVar("run_ctx", default=None)


def current() -> "RunCtx | None":
    return RUN_CTX_VAR.get(None)
