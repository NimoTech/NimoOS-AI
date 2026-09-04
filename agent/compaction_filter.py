"""Mid-run compaction (spec §5): rewrite the OUTGOING copy of the model input
before every model call. Three levels, cheapest first:

  L1 micro_compact   — no LLM: old tool outputs → short placeholder (+file),
                       old reasoning → one-line stub
  L2 rolling summary — one LLM call folding old turns into RunCtx.summary
  hard truncate_turns — drop the oldest turns, keep the first user message

Everything here works on tool-turn boundaries (context_compaction.turn_starts)
because a scheduled-task session has exactly one user message. Nothing here
mutates the items it receives: the SDK builds to_input_list() from the same
dicts, and persisted history must stay the original (spec §5.5 revised).
Any exception → the input is returned unchanged.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

import context_compaction as cc
import tool_output as to

_LOG = logging.getLogger("nimoos-agent.compaction")

MICRO_PLACEHOLDER_HEAD = 300
_COMPACTED_RE = re.compile(r"\[earlier tool output compacted: chars=\d+")
_SYNTHETIC_ID = "__synthetic__"


def _call_names(items) -> dict[str, str]:
    names: dict[str, str] = {}
    for m in items:
        if isinstance(m, dict) and m.get("type") == "function_call":
            cid = str(m.get("call_id") or m.get("id") or "")
            if cid:
                names[cid] = str(m.get("name") or "")
    return names


def _recent_output_boundary(items, keep_recent_results: int) -> int:
    """Index of the start of the turn containing the keep_recent_results-th
    function_call_output from the end; that turn and everything after it are
    recent. Snapped to the turn start so a kept turn's reasoning/function_call
    aren't stubbed while its output survives. Fewer outputs → len(items)
    (nothing old)."""
    seen = 0
    idx = None
    for i in range(len(items) - 1, -1, -1):
        m = items[i]
        if isinstance(m, dict) and m.get("type") == "function_call_output":
            seen += 1
            if seen >= keep_recent_results:
                idx = i
                break
    if idx is None:
        return len(items)
    starts = cc.turn_starts(items)
    return max([s for s in starts if s <= idx], default=idx)


def _compact_output(m: dict, tool_name: str, keep_chars: int) -> dict | None:
    out = m.get("output")
    if not isinstance(out, str) or len(out) <= keep_chars:
        return None
    if _COMPACTED_RE.search(out):
        return None                                  # already compacted earlier this run
    head = out[:MICRO_PLACEHOLDER_HEAD]
    trailer = to.TRAILER_RE.search(out)
    if trailer:
        text = f"{head}\n…\n{trailer.group(0)}"
    else:
        cid = str(m.get("call_id") or m.get("id") or "")
        path = ""
        if cid:
            d = to.OFFLOAD_DIR_VAR.get("")
            existing = os.path.join(d, f"{cid}.txt") if d else ""
            if existing and os.path.isfile(existing):
                path = existing                      # already offloaded this run
            else:
                path = to.store_output(out, call_id=cid, tool_name=tool_name)
        if path:
            text = (f"{head}\n[earlier tool output compacted: chars={len(out)} path={path} — "
                    f"read_file_lines(path, start, end) to revisit]")
        else:
            text = (f"{head}\n[earlier tool output compacted: chars={len(out)}; "
                    f"re-run the tool if you need it again]")
    new = dict(m)
    new["output"] = text
    return new


def micro_compact(items: list, *, keep_recent_results: int = cc.KEEP_RECENT_TOOL_RESULTS,
                  keep_chars: int = cc.MICRO_KEEP_CHARS) -> tuple[list, int]:
    boundary = _recent_output_boundary(items, keep_recent_results)
    if boundary <= 0:
        return items, 0
    names = _call_names(items)
    replaced = 0
    out_items: list[Any] = []
    for i, m in enumerate(items):
        if i >= boundary or not isinstance(m, dict):
            out_items.append(m)
            continue
        t = m.get("type")
        if t == "function_call_output":
            cid = str(m.get("call_id") or m.get("id") or "")
            new = _compact_output(m, names.get(cid, ""), keep_chars)
            if new is not None:
                out_items.append(new)
                replaced += 1
                continue
        elif t == "reasoning" and m.get("id") != _SYNTHETIC_ID:
            summ = m.get("summary")
            already = (isinstance(summ, list) and len(summ) == 1
                       and isinstance(summ[0], dict) and summ[0].get("text") == "(reasoning compacted)")
            if not already:
                new = dict(m)
                new["summary"] = [{"type": "summary_text", "text": "(reasoning compacted)"}]
                out_items.append(new)
                continue
        out_items.append(m)
    if replaced == 0 and out_items == items:
        return items, 0
    return out_items, replaced


def truncate_turns(items: list, *, keep_turns: int) -> list:
    """Keep the leading user message (task prompt / first question) and the
    last keep_turns tool turns. Used only when L1+L2 still leave the input
    over HARD_THRESHOLD."""
    if not items:
        return items
    cut = cc.cut_keep_recent_turns(items, keep_turns)
    if cut <= 0:
        return items
    head = [items[0]] if isinstance(items[0], dict) and items[0].get("role") == "user" else []
    return head + list(items[cut:])


def estimate_since(items: list, from_idx: int) -> int:
    from_idx = max(0, min(from_idx, len(items)))
    return cc.estimate_messages_tokens(items[from_idx:])
