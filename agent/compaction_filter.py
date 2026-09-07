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

from agents import RunHooks
from agents.run import ModelInputData

import context_compaction as cc
import offload_summary as _os
import run_context as rc
import tool_output as to

_LOG = logging.getLogger("nimoos-agent.compaction")

MICRO_PLACEHOLDER_HEAD = 300
REASONING_KEEP_CHARS = 200
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


# NOTE: snapping to turn_starts protects a turn with several parallel outputs
# WHOLE — such a turn may push the kept count above keep_recent_results
# (e.g. 3 parallel calls in the boundary turn keeps 10 outputs when asked for
# 8). That's intended: splitting one turn's outputs would stub a still-live
# tool result the model is about to reason over.
def _recent_output_boundary(items, keep_recent_results: int) -> int:
    """Index of the start of the turn containing the keep_recent_results-th
    function_call_output from the end; that turn and everything after it are
    recent. Snapped to the turn start so a kept turn's reasoning/function_call
    aren't stubbed while its output survives. Fewer outputs than
    keep_recent_results → 0 (nothing old — everything is recent)."""
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
        return 0
    starts = cc.turn_starts(items)
    return max([s for s in starts if s <= idx], default=idx)


def _compact_output(m: dict, tool_name: str, keep_chars: int) -> dict | None:
    out = m.get("output")
    if not isinstance(out, str) or len(out) <= keep_chars:
        return None
    if _COMPACTED_RE.search(out):
        return None                                  # already compacted earlier this run
    head = out[:MICRO_PLACEHOLDER_HEAD]
    head_part = head
    if "<untrusted-data" in head and "</untrusted-data>" not in head:
        # The head cut can land inside a fenced placeholder (P1's own, or any
        # tool output that opens one) — close the fence ourselves so what
        # follows is never read as still being inside untrusted data.
        head_part = head + "\n</untrusted-data>"
    trailer = to.TRAILER_RE.search(out)
    guide = _os.SUMMARY_BLOCK_RE.search(out) if trailer else None
    if guide:
        # P1 placeholder that carries a reading guide: keep the whole guide
        # (it is the useful part) and drop only the advice text after the
        # trailer. Idempotent — a second pass sees the same compact form.
        text = f"{guide.group(0)}\n{trailer.group(0)}"
        if out.strip() == text:
            return None
    elif trailer:
        text = f"{head_part}\n…\n{trailer.group(0)}"
    else:
        cid = str(m.get("call_id") or m.get("id") or "")
        path = ""
        if cid:
            existing = ""
            if to.is_safe_call_id(cid):               # build/check the reuse path
                d = to.OFFLOAD_DIR_VAR.get("")         # only for a safe id — an
                existing = os.path.join(d, f"{cid}.txt") if d else ""  # unsafe
            if existing and os.path.isfile(existing):  # id skips straight to
                path = existing                        # store_output, which
            else:                                      # itself refuses unsafe ids.
                path = to.store_output(out, call_id=cid, tool_name=tool_name)
        if path:
            text = (f"{head_part}\n[earlier tool output compacted: chars={len(out)} path={path} — "
                    f"read_file_lines(path, start, end) to revisit]")
        else:
            text = (f"{head_part}\n[earlier tool output compacted: chars={len(out)}; "
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
                       and isinstance(summ[0], dict)
                       and str(summ[0].get("text") or "").endswith("…(reasoning compacted)"))
            if not already:
                original_text = cc._message_text(m)
                if original_text:
                    head = original_text[:REASONING_KEEP_CHARS]
                    new = dict(m)
                    new["summary"] = [{"type": "summary_text",
                                        "text": head + " …(reasoning compacted)"}]
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


def budget(ctx: rc.RunCtx, items: list) -> int:
    """Estimated total input tokens for the upcoming call (spec §5.2). Prefer
    the provider's own last-call count plus only what changed since (cheap,
    accurate); fall back to a full estimate when there is no provider number
    yet (first call, or after a fold shifted the base — see compaction_filter)."""
    if ctx.last_input_tokens > 0:
        return ctx.last_input_tokens + estimate_since(items, ctx.items_seen_at_last_call)
    return ctx.overhead_tokens + cc.estimate_tokens(ctx.summary) + cc.estimate_messages_tokens(items)


def _estimate(ctx: rc.RunCtx, items: list) -> int:
    """Full estimate, used to recheck the budget after L1/L2 mutate `items`
    within one call — the provider's last-call count is stale at that point."""
    return ctx.overhead_tokens + cc.estimate_tokens(ctx.summary) + cc.estimate_messages_tokens(items)


def _with_summary(ctx: rc.RunCtx, instructions: str | None) -> str | None:
    """Append the current summary block to the BASE instructions, never
    stacking onto a block appended by a previous call. `ctx.extra
    ["base_instructions"]` is captured once (on the first call that ever
    reaches here) so re-running this on the same ctx is idempotent.

    agent.py (Task 6) is expected to pre-seed `ctx.extra["base_instructions"]`
    with the pre-block prompt before the first filter call, so this function
    normally just reads it back. The `SUMMARY_HEADER` strip below is a
    fallback for a caller that skips that seeding (e.g. a test, or a future
    caller) and whose `instructions` already contains a block from a prior
    run/session — without it that stale block would be captured as part of
    "base" and re-appended on every call, alongside the fresh one."""
    if not ctx.summary:
        return instructions
    base = ctx.extra.get("base_instructions")
    if base is None:
        base = instructions or ""
        if cc.SUMMARY_HEADER in base:
            base = base[:base.index(cc.SUMMARY_HEADER)].rstrip()
        ctx.extra["base_instructions"] = base
    block = cc.summary_block(ctx.summary, recall_hint=bool(ctx.extra.get("recall_hint")))
    return f"{base}\n\n{block}" if block else base


async def compaction_filter(data):
    """SDK model-input filter (spec §5): rewrite a COPY of the outgoing
    input/instructions before every model call. Never mutates `data` or the
    persisted history; never raises — any failure returns the untouched
    model_data."""
    md = data.model_data
    ctx = rc.current()
    if ctx is None or not ctx.compaction_enabled:
        return md
    try:
        full = list(md.input or [])
        ctx.extra["last_sent_len"] = len(full)
        base_fold = ctx.fold_idx
        items = full[base_fold:] if 0 < base_fold <= len(full) else full
        W = max(int(ctx.window), 1)
        # max(provider, estimate): the provider's last-call count catches
        # things the char-ratio estimate can't see (tool-schema growth,
        # actual tokenizer behavior), but it reflects the PREVIOUS call's
        # OUTGOING (post-compaction) items while `items` here are the
        # originals again — after an L1 pass, budget()'s provider path
        # (last_input_tokens + only-what's-new-since) understates the true
        # size of the un-compacted list by exactly the L1 savings, which
        # would make L1/hard skip alternate calls (oscillation, possible
        # mid-run 400 on the call that never gets compacted). Estimating
        # both and taking the max means neither blind spot wins.
        est = max(budget(ctx, items), _estimate(ctx, items)) if base_fold == 0 else _estimate(ctx, items)

        if est > cc.L1_THRESHOLD * W:
            before = items
            items, n = micro_compact(items)
            ctx.l1_count += n
            ctx.l1_reasoning_count += sum(
                1 for o, c in zip(before, items)
                if isinstance(o, dict) and isinstance(c, dict)
                and o.get("type") == "reasoning" and c.get("type") == "reasoning"
                and o.get("summary") != c.get("summary"))
            est = _estimate(ctx, items)

        # L1's decision above may be provider-scaled (via budget()); L2 and
        # hard below are always estimate-scaled (_estimate) — accepted, since
        # by this point `items` has already been mutated by L1 and the
        # provider has no number for that shape yet.
        if est > cc.L2_THRESHOLD * W and ctx.summarize_fn is not None and not ctx.l2_disabled:
            cut = cc.cut_keep_recent_turns(items, cc.RECENT_TOOL_TURNS)
            if cut > 0:
                fold_text = "\n".join(
                    cc._message_text(m, max_output_chars=cc.SUMMARY_OUTPUT_MAX_CHARS) for m in items[:cut])
                out = await ctx.summarize_fn(cc.SUMMARIZE_INSTRUCTION, ctx.summary, fold_text)
                if out and out.strip() and cc.estimate_tokens(out) < cc.estimate_tokens(fold_text):
                    ctx.summary = out.strip()
                    ctx.fold_idx = base_fold + cut
                    ctx.l2_count += 1
                    ctx.l2_fail_count = 0
                    items = items[cut:]
                    est = _estimate(ctx, items)
                else:
                    # Empty summary or rejected by the bloat gate — back off
                    # after two failures so a broken/rate-limited summarizer
                    # doesn't eat a timeout budget on every remaining call
                    # this run; hard truncation still covers the overflow.
                    ctx.l2_fail_count += 1
                    if ctx.l2_fail_count >= 2:
                        ctx.l2_disabled = True
                        _LOG.warning("compaction: L2 disabled for this run after 2 failed summaries")

        if est > cc.HARD_THRESHOLD * W:
            items = truncate_turns(items, keep_turns=2)
            # truncate_turns only re-adds a leading user message if items[0]
            # (its own input) already is one — true on a first-call hard
            # truncation, but not after an L2 fold has already dropped the
            # prefix containing it. Re-attach the FULL list's original first
            # user message (the task prompt / initial question) here so a
            # folded-then-truncated run doesn't lose it.
            head_user = (full[0] if full and isinstance(full[0], dict)
                        and full[0].get("role") == "user" else None)
            if head_user is not None and (not items or items[0] is not head_user):
                items = [head_user] + list(items)
            ctx.trunc_count += 1

        return ModelInputData(input=items, instructions=_with_summary(ctx, md.instructions))
    except Exception:  # noqa: BLE001 — SDK re-raises filter errors; never fail the run
        _LOG.warning("compaction_filter bypassed after error", exc_info=True)
        return md


class ContextHooks(RunHooks):
    """Records the provider's real input usage after every model call (spec §5.1)."""

    async def on_llm_end(self, context, agent, response) -> None:
        ctx = rc.current()
        if ctx is None:
            return
        try:
            usage = getattr(response, "usage", None)
            tokens = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
            if tokens <= 0:
                return
            ctx.last_input_tokens = tokens
            ctx.peak_input_tokens = max(ctx.peak_input_tokens, tokens)
            ctx.items_seen_at_last_call = int(ctx.extra.get("last_sent_len", 0) or 0)
            if ctx.conn is not None:
                ctx.conn.execute("UPDATE sessions SET last_real_input_tokens=? WHERE id=?",
                                 (tokens, ctx.session_id))
                ctx.conn.commit()
        except Exception:  # noqa: BLE001
            _LOG.debug("ContextHooks.on_llm_end failed", exc_info=True)
