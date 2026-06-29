"""Single-session context compaction (P4). Main-path, synchronous, never
blocks/breaks chat: any failure → truncate or no-op. Architecture B (lossy
tail): only sessions.rolling_summary is persisted; the SDK's save path is
untouched (feeding the compacted tail makes the saved snapshot the tail).
Summary is injected into the system prompt (instructions), never into the
messages array. Summarization uses the conversation's own model (no new
model)."""
from __future__ import annotations

import logging

import memory_store

_LOG = logging.getLogger("nimoos-agent.context_compaction")

THRESHOLD = 0.70
DEFAULT_CONTEXT_WINDOW = 8192
RECENT_TURNS = 6
COMPACT_LLM_TIMEOUT = 60
SAFETY_MARGIN = 1.15

# substring (lowercased) → context window (tokens); first containment match wins
MODEL_WINDOW_MAP = {
    "gpt-4o": 128000,
    "gpt-4.1": 128000,
    "o1": 128000,
    "o3": 128000,
    "deepseek": 64000,
    "qwen": 32768,
    "claude": 200000,
    "gemini-1.5": 1000000,
    "gemini-2": 1000000,
}

SUMMARY_HEADER = "[对话历史摘要(较早内容已压缩)]"


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF
            or 0x3040 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7AF)


def estimate_tokens(text: str) -> int:
    """Char-ratio token estimate with safety margin. CJK ≈ 1 token/char;
    other ≈ 1 token / 4 chars. Multiplied by SAFETY_MARGIN (code/JSON dense
    text really tokenizes higher than 1/4 — prefer over- to under-estimate)."""
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    raw = cjk + (other + 3) // 4
    return int(raw * SAFETY_MARGIN)


def _message_text(m) -> str:
    """Readable text of one history item (role + textified content),
    robust to multimodal list content."""
    if not isinstance(m, dict):
        return str(m)
    role = m.get("role", "")
    content = m.get("content", "")
    if isinstance(content, str):
        body = content
    elif isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(str(b.get("text") or b.get("content") or ""))
            else:
                parts.append(str(b))
        body = " ".join(p for p in parts if p)
    else:
        body = str(content)
    return f"{role}: {body}"


def estimate_messages_tokens(messages) -> int:
    return sum(estimate_tokens(_message_text(m)) for m in (messages or []))


def resolve_window(conn, user_id, model_name) -> int:
    """user_settings.context_window (positive int) > MODEL_WINDOW_MAP substring
    match > DEFAULT_CONTEXT_WINDOW."""
    user_w = memory_store.get_context_window(conn, user_id)
    if user_w:
        return user_w
    name = (model_name or "").lower()
    for key, win in MODEL_WINDOW_MAP.items():
        if key in name:
            return win
    return DEFAULT_CONTEXT_WINDOW
