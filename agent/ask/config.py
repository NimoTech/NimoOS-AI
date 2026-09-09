"""Knobs for the knowledge-ask pipeline (spec 2026-09-08-knowledge-ask-agent-design §3.3).

Every number lives here so tests and operators have one place to look.
Module-level names are constants read at import time; the switches
(pipeline_enabled, step_summary_mode) are read at call time, so an operator
or a test can flip them without reimporting.
"""
from __future__ import annotations

import os


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


EVIDENCE_MAX_ITEMS = _int_env("NIMOOS_ASK_EVIDENCE_MAX_ITEMS", 10)
EVIDENCE_BUDGET_CHARS = _int_env("NIMOOS_ASK_EVIDENCE_BUDGET_CHARS", 24000)
PIPELINE_TIMEOUT_S = float(os.environ.get("NIMOOS_ASK_PIPELINE_TIMEOUT_S", "") or 45.0)
PER_QUERY_TIMEOUT_S = 15.0
# 10s, not 12: the rewrite is serial with retrieval inside the 15s p50
# answer-start budget (G1), and a timeout falls straight through to the
# deterministic plan rather than paying for a second round trip. Raised from
# 8s: a thinking-disabled one-shot call still measures ~5.2s live, leaving too
# thin a margin at 8s once the session-fallback path (no background model
# configured) is in play.
REWRITE_TIMEOUT_S = 10.0
REWRITE_MAX_TOKENS = 400
STEP_SUMMARY_MAX_TOKENS = 120
# Step summaries are a nicety, the finished evidence pack is not: they run in
# parallel, each capped at STEP_SUMMARY_TIMEOUT_S, and are not started at all
# unless STEP_SUMMARY_MIN_REMAINING_S of the pipeline budget is still left
# (minus the STEP_SUMMARY_RESERVE_S kept for rendering and persistence).
STEP_SUMMARY_TIMEOUT_S = 10.0
STEP_SUMMARY_MIN_REMAINING_S = 8.0
STEP_SUMMARY_RESERVE_S = 5.0
SEARCH_TOP_K = 10
NOTES_TOP_K = 5
RRF_K = 60
NOTE_WEIGHT = 1.2
MECE_MIN_KEEP = 3
PARENT_MAX_CHARS = 6000
MIN_QUERIES, MAX_QUERIES = 2, 5
MAX_QUERY_CHARS = 200

INTENTS = frozenset({"lookup", "list", "compare", "aggregate", "chat"})
SHAPES = frozenset({"value", "list", "table", "prose"})
_SUMMARY_MODES = ("auto", "off", "always")


def pipeline_enabled() -> bool:
    """Master switch: NIMOOS_ASK_PIPELINE=0 turns the search profile into a plain pinned profile."""
    return os.environ.get("NIMOOS_ASK_PIPELINE", "1") != "0"


def step_summary_mode() -> str:
    mode = os.environ.get("NIMOOS_ASK_STEP_SUMMARY", "auto").strip().lower()
    return mode if mode in _SUMMARY_MODES else "auto"


def budget_for_window(window_tokens: int | None) -> int:
    """Evidence budget in chars, shrunk for small context windows (spec §3.6)."""
    if not window_tokens or window_tokens <= 0:
        return EVIDENCE_BUDGET_CHARS
    return min(EVIDENCE_BUDGET_CHARS, int(window_tokens * 1.2))
