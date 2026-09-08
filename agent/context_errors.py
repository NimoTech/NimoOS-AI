"""Recognise "context length exceeded" 400s across providers (spec §6.2)."""
from __future__ import annotations

import json
import re

PATTERNS: tuple[str, ...] = (
    r"context length", r"maximum context", r"context_length_exceeded", r"prompt is too long",
    r"too many tokens", r"input tokens exceed", r"max_tokens[^\n]{0,120}context",
    r"tokens[^\n]{0,120}exceed[^\n]{0,120}limit", r"context window",
    r"exceeds the available context", r"context size",
    # Qwen/DashScope and the Gemini OpenAI-shim wordings (final review Minor 3).
    r"range of input length", r"input length", r"maximum number of tokens",
)
_PATTERN_RE = re.compile("|".join(f"(?:{p})" for p in PATTERNS), re.I)
# Numbers anchored to an explicit limit/window phrase — "maximum context
# length is 131072", "context window of 8192", "limit of 32768", "200000
# maximum" — NEVER a bare unanchored digit scan. A fully unanchored scan (what
# a prior round of this fix used) also matches a completion-token budget
# quoted alongside the request ("...requested N tokens (M in the messages, K
# in the completion)" -> K, always < the real limit) or an unrelated number in
# the body (a request id, a year in a timestamp) — both get persisted by
# model_windows.learn() (which only ever shrinks), silently poisoning that
# model's window forever. Only the number attached to a limit/maximum/window
# phrase is a candidate; if several are found, the limit is never larger than
# what was requested, so MIN is still correct among them.
_ANCHOR_RES: tuple[re.Pattern, ...] = (
    # Drop-in replacement (final review Minor 1): the two separately-optional
    # `\s*` runs around an optional `(?:\(|of\s+)?` group let the backtracker
    # retry every split of a whitespace run between the two `\s*`s against the
    # failing lookahead, which is quadratic in the run length (293 ms measured
    # at the 4 KB text cap). `[\s(]{0,4}` is a single bounded, non-backtracking
    # character class covering the same real-world separators ("is 131072",
    # "of 32768", "(8192)", "is  (4096)") with identical captures.
    re.compile(
        r"(?:maximum context length|context length|context window|context size|limit)"
        r"(?:\s+(?:is|of))?[\s(]{0,4}(?<![\w.])(\d{4,7})(?![\w.])", re.I),
    re.compile(r"(?<![\w.])(\d{4,7})(?![\w.])\s*(?:tokens?)?\s*(?:maximum|max\b|limit)", re.I),
    re.compile(r"limit of (?<![\w.])(\d{4,7})(?![\w.])", re.I),
    # Qwen/DashScope: "Range of input length should be [1, 30720]" and
    # "Input length 35000 exceeds the maximum length 30720" (final review
    # Minor 3) — both report the limit as the LAST number, unlike every other
    # anchored pattern above (which anchors before the number), so these two
    # are their own patterns rather than reusing the generic phrase.
    re.compile(r"input length should be \[\s*\d+\s*,\s*(?<![\w.])(\d{4,7})(?![\w.])\s*\]", re.I),
    re.compile(r"exceeds the maximum length (?<![\w.])(\d{4,7})(?![\w.])", re.I),
    # Gemini OpenAI-shim: "input token count (1050000) exceeds the maximum
    # number of tokens allowed (1000000)" — the limit is the second number.
    re.compile(r"maximum number of tokens allowed[^\n\d]{0,20}\(?(?<![\w.])(\d{4,7})(?![\w.])", re.I),
)
# Fallback when no phrase anchors a number at all: the classic "N tokens"
# wording (the pre-anchor-fix regex) — still requires the word "tokens" right
# after the number, so it does not pick up parenthetical budget breakdowns or
# bare IDs/years, only genuinely token-denominated counts. re.I restored
# (final review Minor 2): a capitalised "8192 Tokens" was not matching.
_TOKENS_RE = re.compile(r"(?<![\w.])(\d{4,7})(?![\w.])\s*tokens", re.I)
_MAX_TEXT_LEN = 4000


class ContextLimitError(Exception):
    def __init__(self, original: BaseException, matched: str, window: int | None):
        super().__init__(f"context limit exceeded ({matched}); window={window}")
        self.original = original
        self.matched = matched
        self.window = window


def _text_of(exc) -> str:
    """Text to scan for a context-limit pattern, capped so a provider echoing
    an arbitrarily large prompt/body back in the error can't blow up the
    (already linear-time) regex work below."""
    parts = [str(getattr(exc, "message", "") or ""), str(exc)]
    body = getattr(exc, "body", None)
    if body is not None:
        try:
            parts.append(json.dumps(body, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            parts.append(str(body))
    text = "\n".join(p for p in parts if p)
    return text[:_MAX_TEXT_LEN]


def classify(exc, *, last_input_tokens: int = 0) -> ContextLimitError | None:
    """ContextLimitError when `exc` is an OpenAI BadRequestError (400) whose
    text matches a context-limit pattern; else None. Never raises."""
    try:
        from openai import BadRequestError  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(exc, BadRequestError):
        return None
    try:
        text = _text_of(exc)
        m = _PATTERN_RE.search(text)
        if not m:
            return None
        anchored = [int(n) for r in _ANCHOR_RES for n in r.findall(text)]
        if anchored:
            window = min(anchored)
        else:
            toks = [int(n) for n in _TOKENS_RE.findall(text)]
            window = min(toks) if toks else (int(last_input_tokens * 0.9) if last_input_tokens > 0 else None)
        return ContextLimitError(exc, m.group(0), window)
    except Exception:  # noqa: BLE001
        return None
