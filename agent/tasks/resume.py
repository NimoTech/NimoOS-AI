"""Compose the continuation instruction for a resumed task run.

Built at ENQUEUE time (the continue endpoint), not at claim time: the parent
run's row could be pruned between the two, and the instruction is part of what
the user asked for — it belongs with the request, not the worker.

A run that died mid-turn never reached `_save_history`, so the resumed session
cannot show the agent its own dead turn; the status/error/denied summary
injected here is the compensation.
"""
from __future__ import annotations

import json

# Continuations are terminal-state only: continuing a queued/running run would
# race the active writer of the same session's history.
CONTINUABLE_STATUSES = frozenset({"succeeded", "failed", "timeout"})

# The user's supplement is capped so a pasted log cannot balloon the prompt;
# the previous run's own error/summary excerpts get a tighter cap each.
SUPPLEMENT_MAX_CHARS = 4000
EXCERPT_MAX_CHARS = 1500

_INSTRUCTION = (
    "Absorb what went wrong above and continue the task to completion — the "
    "full earlier conversation of this session is your context. Your FINAL "
    "ANSWER is what gets delivered through the task's notify channel."
)

# Appended only when the task allows agent prompt revision — inviting a tool
# the gate would refuse anyway just burns a turn on the refusal.
_REVISION_INVITE = (
    "If the failure traces back to the task's own prompt (wrong assumptions, "
    "missing constraints, an impossible step), call `update_task_prompt` to "
    "revise it so future scheduled runs do not repeat it; every previous "
    "version is kept and the user can review and revert revisions."
)


def _clip(text: str, cap: int) -> str:
    text = (text or "").strip()
    if len(text) > cap:
        return text[: cap - 1] + "…"
    return text


def compose_resume_message(run, supplement: str = "", *,
                           invite_revision: bool = True) -> str:
    """The full continuation instruction for one parent run.

    The user's supplement rides ABOVE the boilerplate — their words are the
    instruction, everything else is context on how to carry it out.
    """
    lines = ["[Continuation of a previous task run]",
             f"The previous run ended with status: {run['status']}."]
    error = _clip(run["error"], EXCERPT_MAX_CHARS)
    if error:
        lines.append(f"Error: {error}")
    summary = _clip(run["summary"], EXCERPT_MAX_CHARS)
    if summary:
        lines.append(f"Its final answer was: {summary}")
    denied = _denied_lines(run["denied_actions"])
    if denied:
        lines.append("Actions denied during that run: " + "; ".join(denied))

    supplement = _clip(supplement, SUPPLEMENT_MAX_CHARS)
    parts = []
    if supplement:
        parts.append(supplement)
    parts.append("\n".join(lines))
    instruction = _INSTRUCTION
    if invite_revision:
        instruction = f"{_INSTRUCTION} {_REVISION_INVITE}"
    parts.append(instruction)
    return "\n\n".join(parts)


def _denied_lines(denied_json: str) -> list[str]:
    """Render denied_actions rows tersely; malformed JSON renders as nothing
    (the column is runner-written, but a hand-edited row must not 500 the
    continue endpoint)."""
    try:
        entries = json.loads(denied_json or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(entries, list):
        return []
    out = []
    for entry in entries[:10]:
        if not isinstance(entry, dict):
            continue
        # The driver's record shape: {"kind": ..., "detail": ...}.
        kind = str(entry.get("kind") or "").strip()
        detail = str(entry.get("detail") or "").strip()
        line = " ".join(x for x in (kind, detail) if x)
        if line:
            out.append(_clip(line, 200))
    return out
