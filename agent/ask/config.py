"""Knobs for the knowledge-ask pipeline (spec 2026-09-08-knowledge-ask-agent-design §3.3).

Every number lives here so tests and operators have one place to look.
Environment overrides are read at call time (not import time) where a test
or operator may flip them; pure constants are module-level.
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
REWRITE_TIMEOUT_S = 12.0
REWRITE_MAX_TOKENS = 400
STEP_SUMMARY_MAX_TOKENS = 120
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
