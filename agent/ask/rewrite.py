"""Stage 1 of the ask pipeline: question → retrieval plan (spec §3.3.1).

The plan comes from the cheap background model as strict JSON. Anything that
is not a usable plan (timeout, prose, too few queries) falls back to two
deterministic queries — the pipeline never skips retrieval because the
rewrite failed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from ask import config
from ask.prompt import REWRITE_INSTRUCTION

_LOG = logging.getLogger(__name__)

_LANGS = frozenset({"zh", "en", "any"})
_PUNCT_RE = re.compile(r"[^\w一-鿿]+", re.UNICODE)
_STOPWORDS = frozenset("""
a an the of for to in on at by with from is are was were be been what which who
whom whose how why when where does do did can could should would will please tell me
的 了 是 在 和 与 或 吗 呢 啊 请 帮 我 你 他 她 它 这 那 有 什么 哪些 哪个 怎么 如何 是否 一下 关于
""".split())
# CJK text has no word boundaries: these are removed as substrings (longest first)
# before tokenizing. 多少/最大/最高 are deliberately NOT stopwords — they carry the ask.
_CJK_STOPWORDS = sorted((w for w in _STOPWORDS if re.search(r"[一-鿿]", w)), key=len, reverse=True)


@dataclass(frozen=True)
class SubQuery:
    q: str
    lang: str = "any"


@dataclass(frozen=True)
class Plan:
    needs_retrieval: bool
    intent: str
    queries: tuple[SubQuery, ...]
    answer_shape: str
    fallback: bool = False

    def to_payload(self) -> dict:
        return {"intent": self.intent, "answer_shape": self.answer_shape,
                "queries": [{"q": q.q, "lang": q.lang} for q in self.queries]}


def keyword_query(question: str) -> str:
    """Question minus punctuation and stopwords; the deterministic second query."""
    text = question
    for w in _CJK_STOPWORDS:
        text = text.replace(w, " ")
    tokens = [t for t in _PUNCT_RE.split(text) if t]
    kept = [t for t in tokens if t.lower() not in _STOPWORDS]
    return " ".join(kept)


def fallback_plan(question: str) -> Plan:
    q = question.strip()
    kw = keyword_query(q)
    queries = [SubQuery(q, "any")]
    if kw and kw.lower() != q.lower():
        queries.append(SubQuery(kw, "any"))
    return Plan(needs_retrieval=True, intent="lookup", queries=tuple(queries),
                answer_shape="prose", fallback=True)


def _extract_json(raw: str) -> dict | None:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_plan(raw: str, question: str) -> Plan | None:
    """Strict-but-forgiving parse. None = unusable, caller falls back."""
    obj = _extract_json(raw or "")
    if obj is None:
        return None
    needs = bool(obj.get("needs_retrieval", True))
    intent = str(obj.get("intent", "lookup")).strip().lower()
    if intent not in config.INTENTS:
        intent = "chat" if not needs else "lookup"
    shape = str(obj.get("answer_shape", "prose")).strip().lower()
    if shape not in config.SHAPES:
        shape = "prose"
    queries: list[SubQuery] = []
    seen: set[str] = set()
    for item in obj.get("queries") or []:
        if isinstance(item, str):
            item = {"q": item}
        if not isinstance(item, dict):
            continue
        q = " ".join(str(item.get("q", "")).split())[:config.MAX_QUERY_CHARS]
        if not q or q.lower() in seen:
            continue
        seen.add(q.lower())
        lang = str(item.get("lang", "any")).strip().lower()
        queries.append(SubQuery(q, lang if lang in _LANGS else "any"))
        if len(queries) >= config.MAX_QUERIES:
            break
    if not needs:
        return Plan(False, intent, tuple(queries), shape)
    if len(queries) < config.MIN_QUERIES:
        return None
    return Plan(True, intent, tuple(queries), shape)


async def rewrite(question: str, *, complete, history_hint: str = "",
                  timeout: float | None = None) -> Plan:
    """Ask the background model for a plan; one retry; then fallback_plan().

    The retry is spent ONLY on an answer that came back and was unusable
    (prose, malformed JSON, too few queries) — a model that is confused once
    is often fine on the second try. A call that produced nothing (timeout,
    transport error, or the empty string make_summarizer.complete returns
    when it swallows its own timeout) goes straight to the deterministic
    plan: retrying would spend another REWRITE_TIMEOUT_S of the 15s
    answer-start budget for the same likely outcome.
    """
    if complete is None:
        return fallback_plan(question)
    timeout = timeout if timeout is not None else config.REWRITE_TIMEOUT_S
    body = f"[Question]\n{question.strip()}"
    if history_hint:
        body = f"[Recent questions in this conversation]\n{history_hint}\n\n" + body
    for attempt in (1, 2):
        try:
            raw = await asyncio.wait_for(
                complete(REWRITE_INSTRUCTION, body,
                         max_tokens=config.REWRITE_MAX_TOKENS, timeout=timeout),
                timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — never raise out of the rewrite stage
            _LOG.info("ask rewrite attempt %d failed: %r", attempt, exc)
            return fallback_plan(question)
        if not (raw or "").strip():
            _LOG.info("ask rewrite attempt %d returned nothing", attempt)
            return fallback_plan(question)
        plan = parse_plan(raw, question)
        if plan is not None:
            return plan
    return fallback_plan(question)
