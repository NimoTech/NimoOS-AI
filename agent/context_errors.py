"""Recognise "context length exceeded" 400s across providers (spec §6.2)."""
from __future__ import annotations

import json
import re

PATTERNS: tuple[str, ...] = (
    r"context length", r"maximum context", r"context_length_exceeded", r"prompt is too long",
    r"too many tokens", r"input tokens exceed", r"max_tokens.*context", r"tokens.*exceed.*limit",
    r"context window",
)
_PATTERN_RE = re.compile("|".join(f"(?:{p})" for p in PATTERNS), re.I | re.S)
_LIMIT_RE = re.compile(r"(\d{4,7})\s*tokens", re.I)


class ContextLimitError(Exception):
    def __init__(self, original: BaseException, matched: str, window: int | None):
        super().__init__(f"context limit exceeded ({matched}); window={window}")
        self.original = original
        self.matched = matched
        self.window = window


def _text_of(exc) -> str:
    parts = [str(getattr(exc, "message", "") or ""), str(exc)]
    body = getattr(exc, "body", None)
    if body is not None:
        try:
            parts.append(json.dumps(body, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            parts.append(str(body))
    return "\n".join(p for p in parts if p)


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
        nums = [int(n) for n in _LIMIT_RE.findall(text)]
        window = max(nums) if nums else (int(last_input_tokens * 0.9) if last_input_tokens > 0 else None)
        return ContextLimitError(exc, m.group(0), window)
    except Exception:  # noqa: BLE001
        return None
