"""Pure helpers for AI title generation. No I/O -- easy to unit test."""
from __future__ import annotations

_HISTORY_MAX_ITEMS = 6
_HISTORY_MAX_CHARS = 2000
_TITLE_MAX_CHARS = 30
_FALLBACK_MAX_CHARS = 16
_QUOTE_PAIRS = (
    ('"', '"'),
    ("'", "'"),
    ('“', '”'),  # curly quotes U+201C U+201D
    ('「', '」'),
    ('『', '』'),
)


def _flatten_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in (
                "output_text", "text", "input_text", "reasoning_text"
            ):
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


def _is_textual_message(item: dict) -> bool:
    role = item.get("role")
    if role == "user":
        return True
    if role == "assistant" and item.get("type") in ("message", "message_output_item", None):
        return True
    return False


def extract_history_excerpt(history: list) -> str:
    """Pick first N textual user/assistant messages, join, truncate."""
    parts = []
    count = 0
    for item in history:
        if not _is_textual_message(item):
            continue
        text = _flatten_content(item.get("content")).strip()
        if not text:
            continue
        role = item.get("role", "user")
        parts.append(f"{role.upper()}: {text}")
        count += 1
        if count >= _HISTORY_MAX_ITEMS:
            break
    excerpt = "\n".join(parts)
    return excerpt[:_HISTORY_MAX_CHARS]


def clean_llm_title(raw: str) -> str:
    """Strip whitespace/quotes, take first line, hard-truncate."""
    if not raw:
        return ""
    first_line = raw.strip().split("\n", 1)[0].strip()
    # Strip wrapping quotes (both ASCII and CJK styles)
    for open_q, close_q in _QUOTE_PAIRS:
        if (
            len(first_line) >= 2
            and first_line.startswith(open_q)
            and first_line.endswith(close_q)
        ):
            first_line = first_line[1:-1].strip()
            break
    # Strip a trailing common punctuation
    while first_line and first_line[-1] in ".。!！?？":
        first_line = first_line[:-1]
    return first_line[:_TITLE_MAX_CHARS]


def first_user_fallback(history: list) -> str:
    """Use the first non-empty user message's first 16 chars as fallback title."""
    for item in history:
        if item.get("role") == "user":
            text = _flatten_content(item.get("content")).strip()
            if text:
                return text[:_FALLBACK_MAX_CHARS]
    return ""


SYSTEM_PROMPT = (
    "Generate a concise title (<=16 characters, no quotes, no trailing punctuation) "
    "that summarizes the topic of the following conversation. "
    "The title MUST use the same primary language as the conversation. "
    "Return only the title text, nothing else."
)
