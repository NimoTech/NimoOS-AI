"""Single-session context compaction (P4). Main-path, synchronous, never
blocks/breaks chat: any failure → truncate or no-op. Architecture B (lossy
tail): only sessions.rolling_summary is persisted; the SDK's save path is
untouched (feeding the compacted tail makes the saved snapshot the tail).
Summary is injected into the system prompt (instructions), never into the
messages array. Summarization uses the conversation's own model (no new
model)."""
from __future__ import annotations

import asyncio
import json
import logging

import memory_store
import recall_index

_LOG = logging.getLogger("nimoos-agent.context_compaction")

THRESHOLD = 0.70
# Tier defaults: cloud models 256k, local (Ollama) models 8k. The old
# per-family MODEL_WINDOW_MAP proved unmaintainable — every new model fell
# through to a wrong guess. Models whose real window differs are corrected
# via the user override (settings / chat composer), floored so a stray
# tiny value can't put every session into a truncate-each-turn spiral.
CLOUD_CONTEXT_WINDOW = 262144   # 256k
LOCAL_CONTEXT_WINDOW = 8192     # 8k
MIN_CONTEXT_WINDOW = 1024
# Callers with no tier signal (e.g. context-usage with model omitted) are
# treated as cloud.
DEFAULT_CONTEXT_WINDOW = CLOUD_CONTEXT_WINDOW
RECENT_TURNS = 6
COMPACT_LLM_TIMEOUT = 60
SAFETY_MARGIN = 1.15
SUMMARY_OUTPUT_MAX_CHARS = 500   # cap on function_call_output text fed to the
                                 # summarizer (NOT the estimate, which is full)
TOOLS_BASE_OVERHEAD = 80   # fixed framework boilerplate around the tools array
                           # ("You have access to the following tools…")

SUMMARY_HEADER = "[Conversation history summary (earlier content compacted)]"


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


def _message_text(m, *, max_output_chars=None) -> str:
    """Readable text of one history/SDK item. Handles standard {role, content}
    (str or list of blocks) AND the agents SDK shapes with no content:
    function_call (name+arguments), function_call_output (output — the largest
    part), reasoning (summary[].text). Unknown → "" (don't count metadata).

    Estimation calls with max_output_chars=None (full, accurate). The summary
    fold path passes a cap so giant tool outputs don't overflow the summarizer
    (only function_call_output.output is capped)."""
    if not isinstance(m, dict):
        return str(m)
    content = m.get("content")
    if content:                       # user / assistant / type=message
        role = m.get("role", "")
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
    t = m.get("type")
    if t == "function_call":
        return f"{m.get('name', '')}: {m.get('arguments', '') or ''}"
    if t == "function_call_output":
        out = m.get("output")
        if not isinstance(out, str):
            out = json.dumps(out, ensure_ascii=False) if out is not None else ""
        if max_output_chars is not None and len(out) > max_output_chars:
            out = out[:max_output_chars] + f"…[+{len(out) - max_output_chars} chars]"
        return out
    if t == "reasoning":
        summ = m.get("summary")
        if isinstance(summ, list):
            return " ".join(str(b.get("text", "")) for b in summ
                            if isinstance(b, dict))
        return str(summ or "")
    return ""


def estimate_messages_tokens(messages) -> int:
    return sum(estimate_tokens(_message_text(m)) for m in (messages or []))


def _tool_is_sent(t) -> bool:
    """Whether this tool's schema actually goes into the request. Gated tool
    copies carry an is_enabled callback (tool_gating.make_is_enabled) that
    reads the run's UNLOCKED_VAR — a still-locked category's schemas are NOT
    sent, so counting them once inflated a fresh session's overhead by ~8.5k
    (and put every local-8k-window session permanently "over budget").
    is_enabled may be a bool or a sync callback ignoring its (ctx, agent)
    args; anything unexpected (async, raising) counts the tool — estimating
    high is safe, estimating low is not."""
    enabled = getattr(t, "is_enabled", True)
    if isinstance(enabled, bool):
        return enabled
    if callable(enabled):
        try:
            res = enabled(None, None)
        except Exception:
            return True
        if isinstance(res, bool):
            return res
        if asyncio.iscoroutine(res):
            res.close()   # async callback: don't await here, just count it
        return True
    return True


def estimate_tools_tokens(tools) -> int:
    """Estimated tokens of the tool definitions sent to the model each request
    (name + description + JSON-schema params), serialized, plus a fixed base
    for the framework's tool-array boilerplate. Tools whose is_enabled
    resolves False are skipped — the SDK omits them from the request, so
    they cost nothing (mid-run unlocks grow later requests; the THRESHOLD
    headroom absorbs that, same as tool-result growth). getattr-tolerant; a
    tool that fails to serialize is skipped, never raises. Empty/None → 0
    (no base)."""
    import json as _json
    items = [t for t in (tools or []) if _tool_is_sent(t)]
    if not items:
        return 0
    total = TOOLS_BASE_OVERHEAD
    for t in items:
        try:
            spec = {
                "name": getattr(t, "name", "") or "",
                "description": getattr(t, "description", "") or "",
                "parameters": getattr(t, "params_json_schema", {}) or {},
            }
            total += estimate_tokens(_json.dumps(spec, ensure_ascii=False))
        except Exception:
            continue
    return total


def resolve_window(conn, user_id, model_name, provider_type: str = "") -> int:
    """user_settings.context_window (int >= MIN_CONTEXT_WINDOW) > tier
    default: local (Ollama) 8k, everything else (cloud) 256k.

    Local is detected two ways because callers hold different names: chat
    runs send the bare model name plus provider_type ("ollama" for local);
    the context-usage endpoint receives the UI's full selector key
    ("local:<name>" / "cloud:<id>:<name>"). The MIN_CONTEXT_WINDOW floor is
    enforced at the settings write path (a stray saved "2" once put every
    session permanently over budget); reads stay unfloored so tests can
    force compaction with tiny windows."""
    user_w = memory_store.get_context_window(conn, user_id)
    if user_w:
        return user_w
    name = (model_name or "").lower()
    if provider_type == "ollama" or name.startswith("local:"):
        return LOCAL_CONTEXT_WINDOW
    return CLOUD_CONTEXT_WINDOW


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


def _read_summary_state(conn, session_id) -> tuple[str, int]:
    row = conn.execute(
        "SELECT rolling_summary, folded_upto FROM sessions WHERE id=?",
        (session_id,)).fetchone()
    if not row:
        return "", 0
    return (row["rolling_summary"] or ""), (row["folded_upto"] or 0)


def _write_summary_state(conn, session_id, summary, folded_upto) -> None:
    conn.execute(
        "UPDATE sessions SET rolling_summary=?, folded_upto=? WHERE id=?",
        (summary, folded_upto, session_id))
    conn.commit()


RECALL_HINT = "(full earlier conversation text can be retrieved on demand via the recall tool)"


def summary_block(summary, *, recall_hint=False) -> str:
    s = (summary or "").strip()
    if not s:
        return ""
    block = f"{SUMMARY_HEADER}\n{s}"
    return f"{block}\n{RECALL_HINT}" if recall_hint else block


SUMMARIZE_INSTRUCTION = (
    "You are a conversation-history compactor. Merge the [Existing summary] and the [Earlier conversation excerpts] "
    "into one tighter, information-lossless rolling summary: keep key facts, user preferences, decisions made, "
    "open questions, entities and conclusions; drop pleasantries and redundancy. Write the summary in the "
    "conversation's language. Output only the summary text — no explanations, no prefix or suffix."
)


def _truncate_to_fit(send_history, summary_text, current_text, line,
                     overhead=0) -> list:
    """Drop oldest user-turns from send_history (user boundaries) until it fits
    line; always keep at least the last turn."""
    us = _user_indices(send_history)
    if not us:
        return send_history
    base = overhead + estimate_tokens(summary_text) + estimate_tokens(current_text)
    for c in us:                      # ascending user boundaries
        cand = send_history[c:]
        if base + estimate_messages_tokens(cand) <= line:
            return cand
    return send_history[us[-1]:]       # keep only the last turn


async def compact_for_run(conn, *, session_id, user_id, model_name,
                          history, current_text, summarize_fn, now=None,
                          overhead_tokens: int = 0, provider_type: str = ""):
    try:
        if not memory_store.is_compaction_enabled(conn, user_id):
            return "", history
        S, F = _read_summary_state(conn, session_id)
        hint = memory_store.is_memory_enabled(conn, user_id)
        if F < 0 or F > len(history):
            # Stale cursor (manual restore / external edit): fall back to 0
            # rather than slicing history into nonsense.
            _LOG.warning("stale folded_upto=%d for %s (history=%d); using 0",
                         F, session_id, len(history))
            F = 0
        W = resolve_window(conn, user_id, model_name, provider_type)
        line = int(THRESHOLD * W)
        # history[:F] already lives inside S — count only the unfolded part,
        # or a long session would stay "over the line" forever.
        total = (overhead_tokens + estimate_tokens(S)
                 + estimate_messages_tokens(history[F:])
                 + estimate_tokens(current_text))
        if total <= line:
            return summary_block(S, recall_hint=hint), history[F:] if F else history

        new_S, new_F = S, F
        send_history = history[F:]
        cut = keepk_cut(history, RECENT_TURNS)
        if cut > F:
            instr_overhead = estimate_tokens(SUMMARIZE_INSTRUCTION) + estimate_tokens(S)
            # Fold only the [F:cut) delta, shrinking cut (floored at F) until
            # the truncated fold fits the summarizer window W.
            fold_text = ""
            fold_cut = cut
            while fold_cut > F:
                fold_text = "\n".join(
                    _message_text(m, max_output_chars=SUMMARY_OUTPUT_MAX_CHARS)
                    for m in history[F:fold_cut])
                if instr_overhead + estimate_tokens(fold_text) <= W:
                    break
                fold_cut = max(_prev_user_boundary(history, fold_cut), F)
                fold_text = ""
            if fold_cut > F and fold_text:
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
                    new_F = fold_cut
                    _write_summary_state(conn, session_id, new_S, new_F)
                    send_history = history[fold_cut:]
                # else: cursor unchanged; send_history stays history[F:] →
                # terminal truncate below decides what still fits.

        if (overhead_tokens + estimate_tokens(new_S)
                + estimate_messages_tokens(send_history)
                + estimate_tokens(current_text)) > line:
            send_history = _truncate_to_fit(send_history, new_S,
                                            current_text, line,
                                            overhead=overhead_tokens)
        if (overhead_tokens + estimate_tokens(new_S)
                + estimate_messages_tokens(send_history)
                + estimate_tokens(current_text)) > line:
            _LOG.warning(
                "context still over budget after compaction for %s "
                "(overhead=%d, window-line=%d): input too long / too many tools",
                session_id, overhead_tokens, line)

        # Content left the model's context THIS run (new fold or truncation):
        # make it recallable right away instead of waiting for a 120s idle gap
        # that a busy session may never reach. Never blocks / never raises.
        dropped = (new_F > F) or (len(send_history) < len(history) - new_F)
        if dropped:
            try:
                recall_index.maybe_enqueue_index_job(
                    conn, session_id, str(user_id), now=now, immediate=True)
            except Exception:
                _LOG.debug("immediate recall enqueue failed for %s",
                           session_id, exc_info=True)
        return summary_block(new_S, recall_hint=hint), send_history
    except Exception as e:
        _LOG.warning("compaction error, bypassing (%s): %s", session_id, e)
        return "", history


def _load_snapshot_history(conn, session_id) -> list:
    """Latest cumulative history snapshot (role='history', content=json list)
    — same source the run path persists/reads."""
    import json as _json
    row = conn.execute(
        "SELECT content FROM messages WHERE session_id=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1", (session_id,)).fetchone()
    if not row:
        return []
    try:
        h = _json.loads(row["content"])
        return h if isinstance(h, list) else []
    except (ValueError, KeyError):
        return []


def compute_usage(conn, *, session_id, user_id, model) -> dict:
    """Read-only current context usage for a session vs the resolved model
    window. Never writes, never triggers compaction.

    Prefers the provider-reported input_tokens of the last run's final
    request (sessions.last_real_input_tokens, captured when the endpoint
    honours stream_options.include_usage) — the provider's own count of the
    current context. Falls back to the same char-ratio estimator compaction
    uses, so the fallback pct aligns with the THRESHOLD trigger."""
    window = resolve_window(conn, user_id, model)
    source = "estimate"
    try:
        # Scope by user_id: a session is only readable by its owner. A
        # non-existent OR not-owned session returns zeros (no cross-user
        # info leak — IDOR guard), mirroring other user-scoped agent endpoints.
        srow = conn.execute(
            "SELECT rolling_summary, last_overhead_tokens, folded_upto, "
            "last_real_input_tokens "
            "FROM sessions WHERE id=? AND user_id=?",
            (session_id, str(user_id))).fetchone()
        if srow is None:
            tokens = 0
        elif (srow["last_real_input_tokens"] or 0) > 0:
            tokens = srow["last_real_input_tokens"]
            source = "provider"
        else:
            summary = srow["rolling_summary"] or ""
            overhead = srow["last_overhead_tokens"] or 0
            fold_f = srow["folded_upto"] or 0
            history = _load_snapshot_history(conn, session_id)
            if fold_f < 0 or fold_f > len(history):
                fold_f = 0
            tokens = (overhead + estimate_tokens(summary)
                      + estimate_messages_tokens(history[fold_f:]))
    except Exception as e:
        _LOG.warning("context usage compute failed for %s: %s", session_id, e)
        tokens = 0
    pct = round(100 * tokens / window) if window else 0
    return {"tokens": tokens, "window": window, "pct": pct, "source": source}
