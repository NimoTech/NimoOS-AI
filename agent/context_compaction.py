"""Single-session context compaction (P4). Main-path, synchronous, never
blocks/breaks chat: any failure → truncate or no-op. Architecture B (lossy
tail): only sessions.rolling_summary is persisted; the SDK's save path is
untouched (feeding the compacted tail makes the saved snapshot the tail).
Summary is injected into the system prompt (instructions), never into the
messages array. Summarization uses the conversation's own model (no new
model)."""
from __future__ import annotations

import asyncio
import logging
import re

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


def _key_matches(key: str, name: str) -> bool:
    """Substring match for normal keys; short keys (len<4, e.g. 'o1'/'o3')
    require a non-alphanumeric boundary before and a non-digit after, so
    'o1-mini'/'-o1' match but 'do3'/'no1se'/'o13b' do not."""
    if len(key) >= 4:
        return key in name
    return re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?![0-9])",
                     name) is not None


def resolve_window(conn, user_id, model_name) -> int:
    """user_settings.context_window (positive int) > MODEL_WINDOW_MAP match >
    DEFAULT_CONTEXT_WINDOW."""
    user_w = memory_store.get_context_window(conn, user_id)
    if user_w:
        return user_w
    name = (model_name or "").lower()
    for key, win in MODEL_WINDOW_MAP.items():
        if _key_matches(key, name):
            return win
    return DEFAULT_CONTEXT_WINDOW


def _user_indices(history) -> list:
    return [i for i, m in enumerate(history)
            if isinstance(m, dict) and m.get("role") == "user"]


def keepk_cut(history, keep_turns) -> int:
    """Index of the keep_turns-th user message from the end (history[cut:]
    keeps the last keep_turns turns). Fewer than keep_turns users → 0."""
    us = _user_indices(history)
    if len(us) <= keep_turns:
        return 0
    return us[len(us) - keep_turns]


def _prev_user_boundary(history, cut) -> int:
    """Largest user-message index strictly < cut, else 0."""
    us = [i for i in _user_indices(history) if i < cut]
    return us[-1] if us else 0


def _read_summary(conn, session_id) -> str:
    row = conn.execute("SELECT rolling_summary FROM sessions WHERE id=?",
                       (session_id,)).fetchone()
    if not row:
        return ""
    return row["rolling_summary"] or ""


def _write_summary(conn, session_id, summary) -> None:
    conn.execute("UPDATE sessions SET rolling_summary=? WHERE id=?",
                 (summary, session_id))
    conn.commit()


def summary_block(summary) -> str:
    s = (summary or "").strip()
    return f"{SUMMARY_HEADER}\n{s}" if s else ""


SUMMARIZE_INSTRUCTION = (
    "你是对话历史压缩器。把【已有摘要】和【更早的对话片段】融合成一段更紧凑、"
    "信息无损的滚动摘要:保留关键事实、用户偏好、已做的决定、未决问题、实体与"
    "结论;去掉寒暄与冗余。只输出摘要正文,不要解释、不要前后缀。"
)


def _truncate_to_fit(send_history, summary_text, current_text, line) -> list:
    """Drop oldest user-turns from send_history (user boundaries) until it fits
    line; always keep at least the last turn."""
    us = _user_indices(send_history)
    if not us:
        return send_history
    base = estimate_tokens(summary_text) + estimate_tokens(current_text)
    for c in us:                      # ascending user boundaries
        cand = send_history[c:]
        if base + estimate_messages_tokens(cand) <= line:
            return cand
    return send_history[us[-1]:]       # keep only the last turn


async def compact_for_run(conn, *, session_id, user_id, model_name,
                          history, current_text, summarize_fn, now=None):
    try:
        if not memory_store.is_compaction_enabled(conn, user_id):
            return "", history
        S = _read_summary(conn, session_id)
        W = resolve_window(conn, user_id, model_name)
        line = int(THRESHOLD * W)
        total = (estimate_tokens(S) + estimate_messages_tokens(history)
                 + estimate_tokens(current_text))
        if total <= line:
            return summary_block(S), history

        cut = keepk_cut(history, RECENT_TURNS)
        instr_overhead = estimate_tokens(SUMMARIZE_INSTRUCTION) + estimate_tokens(S)
        # window precheck: shrink cut until fold fits the summarizer window W
        while cut > 0 and (instr_overhead
                           + estimate_messages_tokens(history[:cut])) > W:
            cut = _prev_user_boundary(history, cut)

        new_S = S
        send_history = history
        if cut > 0:
            fold_text = "\n".join(_message_text(m) for m in history[:cut])
            out = None
            try:
                out = await asyncio.wait_for(
                    summarize_fn(SUMMARIZE_INSTRUCTION, S, fold_text),
                    timeout=COMPACT_LLM_TIMEOUT)
            except Exception as e:
                _LOG.warning("compaction summarize failed (%s): %s",
                             session_id, e)
            if (out and out.strip()
                    and estimate_tokens(out) < estimate_tokens(fold_text)):
                new_S = out.strip()
                _write_summary(conn, session_id, new_S)
                send_history = history[cut:]
            # else: keep old S, send_history stays = history → terminal truncate

        if (estimate_tokens(new_S) + estimate_messages_tokens(send_history)
                + estimate_tokens(current_text)) > line:
            send_history = _truncate_to_fit(send_history, new_S,
                                            current_text, line)
        return summary_block(new_S), send_history
    except Exception as e:
        _LOG.warning("compaction error, bypassing (%s): %s", session_id, e)
        return "", history
