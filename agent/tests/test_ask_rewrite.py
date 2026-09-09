import asyncio
import json

import pytest

from ask import rewrite as rw


def test_parse_plan_accepts_wrapped_json():
    raw = 'Sure:\n{"needs_retrieval": true, "intent": "compare", "queries": [' \
          '{"q": "Ultra 7 265K max turbo", "lang": "en"}, {"q": "265K 睿频", "lang": "zh"}],' \
          ' "answer_shape": "table"}\nDone.'
    plan = rw.parse_plan(raw, "对比 265K 和 245K 的睿频")
    assert plan is not None and plan.intent == "compare" and plan.answer_shape == "table"
    assert [q.q for q in plan.queries] == ["Ultra 7 265K max turbo", "265K 睿频"]
    assert plan.fallback is False


def test_parse_plan_normalises_bad_enums_and_dedupes():
    raw = json.dumps({"needs_retrieval": True, "intent": "weird", "answer_shape": "???",
                      "queries": [{"q": "a b"}, {"q": "A B", "lang": "en"}, {"q": "  "}, {"q": "c"}]})
    plan = rw.parse_plan(raw, "q")
    assert plan.intent == "lookup" and plan.answer_shape == "prose"
    assert [q.q for q in plan.queries] == ["a b", "c"]
    assert plan.queries[0].lang == "any"


def test_parse_plan_rejects_garbage_and_too_few_queries():
    assert rw.parse_plan("not json", "q") is None
    one = json.dumps({"needs_retrieval": True, "intent": "lookup", "queries": [{"q": "only"}]})
    assert rw.parse_plan(one, "q") is None          # < MIN_QUERIES → caller falls back


def test_parse_plan_no_retrieval_allows_empty_queries():
    raw = json.dumps({"needs_retrieval": False, "intent": "chat", "queries": [], "answer_shape": "prose"})
    plan = rw.parse_plan(raw, "hello")
    assert plan is not None and plan.needs_retrieval is False and plan.queries == ()


def test_parse_plan_caps_query_count_and_length():
    qs = [{"q": f"query number {i} " + "x" * 300} for i in range(9)]
    plan = rw.parse_plan(json.dumps({"needs_retrieval": True, "queries": qs}), "q")
    assert len(plan.queries) == rw.config.MAX_QUERIES
    assert all(len(q.q) <= rw.config.MAX_QUERY_CHARS for q in plan.queries)


def test_keyword_query_strips_stopwords_and_punctuation():
    assert rw.keyword_query("What is the max turbo frequency of the Core Ultra 7 265K?") \
        == "max turbo frequency Core Ultra 7 265K"
    assert rw.keyword_query("酷睿 Ultra 7 265K 的最大睿频是多少？") == "酷睿 Ultra 7 265K 最大睿频 多少"


def test_fallback_plan_has_original_plus_keywords():
    plan = rw.fallback_plan("What is the TDP of 265K?")
    assert plan.fallback is True and plan.needs_retrieval is True and plan.intent == "lookup"
    assert [q.q for q in plan.queries] == ["What is the TDP of 265K?", "TDP 265K"]


def test_fallback_plan_collapses_when_keywords_equal_question():
    plan = rw.fallback_plan("265K TDP")
    assert [q.q for q in plan.queries] == ["265K TDP"]


@pytest.mark.asyncio
async def test_rewrite_retries_once_then_falls_back():
    calls = []

    async def complete(instruction, body, *, max_tokens, timeout):
        calls.append(body)
        return "garbage"

    plan = await rw.rewrite("What is the TDP of 265K?", complete=complete)
    assert len(calls) == 2 and plan.fallback is True


@pytest.mark.asyncio
async def test_rewrite_uses_model_plan_and_history_hint():
    seen = {}

    async def complete(instruction, body, *, max_tokens, timeout):
        seen["body"] = body
        return json.dumps({"needs_retrieval": True, "intent": "lookup",
                           "queries": [{"q": "a"}, {"q": "b"}], "answer_shape": "value"})

    plan = await rw.rewrite("and its TDP?", complete=complete, history_hint="Q1: 265K max turbo")
    assert plan.fallback is False and [q.q for q in plan.queries] == ["a", "b"]
    assert "Q1: 265K max turbo" in seen["body"] and "and its TDP?" in seen["body"]


@pytest.mark.asyncio
async def test_rewrite_without_complete_falls_back():
    plan = await rw.rewrite("anything here", complete=None)
    assert plan.fallback is True


@pytest.mark.asyncio
async def test_rewrite_timeout_falls_back():
    async def slow(instruction, body, *, max_tokens, timeout):
        await asyncio.sleep(0.2)
        return "{}"

    plan = await rw.rewrite("slow question", complete=slow, timeout=0.01)
    assert plan.fallback is True


@pytest.mark.asyncio
async def test_rewrite_timeout_does_not_retry():
    """A second round trip after a timeout costs another REWRITE_TIMEOUT_S of
    the 15s answer-start budget and rarely helps (G1)."""
    calls = []

    async def slow(instruction, body, *, max_tokens, timeout):
        calls.append(body)
        await asyncio.sleep(0.5)
        return "{}"

    plan = await rw.rewrite("slow question", complete=slow, timeout=0.01)
    assert plan.fallback is True and len(calls) == 1


@pytest.mark.asyncio
async def test_rewrite_empty_answer_does_not_retry():
    """make_summarizer's complete() swallows its own timeout and returns "";
    that is a failed call, not an unusable answer, so it must not be retried."""
    calls = []

    async def empty(instruction, body, *, max_tokens, timeout):
        calls.append(body)
        return "   "

    plan = await rw.rewrite("q", complete=empty)
    assert plan.fallback is True and len(calls) == 1


def test_rewrite_timeout_default_fits_the_answer_budget():
    assert rw.config.REWRITE_TIMEOUT_S == 10.0
