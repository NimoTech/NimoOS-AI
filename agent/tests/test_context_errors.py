import time

import httpx
import pytest
from openai import BadRequestError, RateLimitError

import context_compaction as cc
import context_errors as ce
import model_windows as mw
from db import init_db


def _bad_request(msg: str, body=None):
    req = httpx.Request("POST", "https://api.x/v1/chat/completions")
    resp = httpx.Response(400, request=req, json=body or {"error": {"message": msg}})
    return BadRequestError(msg, response=resp, body=body or {"error": {"message": msg}})


@pytest.mark.parametrize("msg,expected_window", [
    ("This model's maximum context length is 131072 tokens. However, you requested 140000 tokens", 131072),
    ("context_length_exceeded: prompt is too long", None),
    ("Input tokens exceed the configured limit of 32768 tokens", 32768),
    ("prompt is too long: 210000 tokens > 200000 maximum", 200000),
    ("too many tokens in the request", None),
    ("max_tokens is too large for the remaining context", None),
    ("Requested tokens (9000) exceed the context window limit (8192)", 8192),
    # Anchored-first (Major 3 fix-round-2): the limit, never the completion
    # budget quoted alongside the request.
    ("This model's maximum context length is 128000 tokens. However, you requested "
     "130000 tokens (129000 in the messages, 1000 in the completion)", 128000),
    ("prompt is too long (request id 2026-09-08T10:00:00Z)", None),   # a bare year is not a limit
    ("maximum context length is 32768 tokens. However, you requested 40000 tokens in the messages", 32768),
])
def test_classify_matches_context_messages(msg, expected_window):
    err = ce.classify(_bad_request(msg), last_input_tokens=0)
    assert isinstance(err, ce.ContextLimitError)
    assert err.window == expected_window
    assert err.original is not None and err.matched


def test_classify_falls_back_to_90pct_of_last_input():
    err = ce.classify(_bad_request("context_length_exceeded"), last_input_tokens=100_000)
    assert err is not None and err.window == 90_000


def test_classify_ignores_unrelated_400_and_other_errors():
    assert ce.classify(_bad_request("invalid tool_calls: insufficient tool messages")) is None
    assert ce.classify(RuntimeError("context length exceeded")) is None      # not a BadRequestError
    req = httpx.Request("POST", "https://api.x"); resp = httpx.Response(429, request=req, json={})
    assert ce.classify(RateLimitError("too many tokens", response=resp, body={})) is None
    assert ce.classify(None) is None


def test_classify_reads_body_when_message_is_generic():
    err = ce.classify(_bad_request("Error code: 400", body={"error": {"message": "maximum context length is 65536 tokens"}}))
    assert err is not None and err.window == 65536


def test_classify_matches_ollama_llamacpp_context_size_message():
    err = ce.classify(_bad_request("the request exceeds the available context size"))
    assert isinstance(err, ce.ContextLimitError) and err.matched


def test_classify_is_fast_on_whitespace_heavy_body_at_the_4kb_cap():
    # Final review Minor 1: the anchored-limit regex's two independently
    # optional `\s*` runs backtracked quadratically on a whitespace run
    # between the anchor phrase and the number — 293 ms measured on this
    # exact shape at the 4 KB text cap (_MAX_TEXT_LEN). The bounded
    # `[\s(]{0,4}` replacement must stay well under that.
    msg = "maximum context length is" + (" " * 3900) + "131072"
    exc = _bad_request(msg)
    t0 = time.perf_counter()
    result = ce.classify(exc)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.02, f"classify() took {elapsed:.4f}s on whitespace-heavy body"
    assert result is None or isinstance(result, ce.ContextLimitError)


@pytest.mark.parametrize("msg,expected_window", [
    # Final review §3 probe table — real provider wordings, re-verified
    # against the fixed patterns.
    ("This model's maximum context length is 128000 tokens. However, you "
     "requested 130000 tokens. Please reduce the length of the messages.", 128000),
    ("This model's maximum context length is 8192 tokens. However, you requested "
     "8500 tokens (300 in the messages, 8200 in the completion).", 8192),
    ("This model's maximum context length is 4096 tokens, however you requested "
     "5000 tokens (4500 in your prompt; 500 for the completion)", 4096),
    ("This model's maximum context length is 65536 tokens. However, you requested "
     "70000 tokens (69000 in the messages, 1000 in the completion)", 65536),
    ("The maximum context length is 32768 tokens, but the messages resulted in "
     "33000 tokens", 32768),
    ("This model's maximum context length is 32768 tokens. However, you requested "
     "33000 tokens", 32768),
    ("prompt is too long: 210000 tokens > 200000 maximum", 200000),
    ("litellm.BadRequestError: AnthropicException - {\"error\":{\"type\":\"invalid_request_error\","
     "\"message\":\"prompt is too long: 210000 tokens > 200000 maximum\"}}", 200000),
    ("Please reduce your prompt; or completion length. The model context length is "
     "8192 tokens, however you requested 8300 tokens", 8192),
    # Qwen/DashScope and the Gemini OpenAI-shim (Minor 3) — previously
    # unrecognised, now learn the real limit.
    ("Range of input length should be [1, 30720]", 30720),
    ("Input length 35000 exceeds the maximum length 30720", 30720),
    ("The input token count (1050000) exceeds the maximum number of tokens "
     "allowed (1000000).", 1000000),
])
def test_classify_learns_real_provider_wordings(msg, expected_window):
    err = ce.classify(_bad_request(msg), last_input_tokens=0)
    assert isinstance(err, ce.ContextLimitError)
    assert err.window == expected_window


@pytest.mark.parametrize("msg", [
    # Fallback shape (Volces/ark prose, llama.cpp, Ollama-style, Mistral):
    # no anchored number at all → 0.9 * last_input_tokens, never poisoned by
    # an unrelated digit in the body.
    "input tokens exceeded the model's context length limit",
    "the request exceeds the available context size, please increase context "
    "or reduce prompt length",
    "input length exceeds context length",
    "Too many tokens in prompt: 40000 > 32768",
])
def test_classify_falls_back_to_90pct_for_numberless_or_unparsed_wordings(msg):
    err = ce.classify(_bad_request(msg), last_input_tokens=50_000)
    assert isinstance(err, ce.ContextLimitError)
    assert err.window == 45_000


def test_classify_tokens_pattern_is_case_insensitive():
    # Minor 2 (final review): re.I was lost on the tier-2 "N tokens"
    # fallback — a capitalised "Tokens" stopped parsing.
    err = ce.classify(_bad_request("prompt is too long: 8192 Tokens"))
    assert isinstance(err, ce.ContextLimitError) and err.window == 8192


def test_classify_is_linear_time_on_adversarial_body():
    # A provider echoing a very long prompt back in the error body must not
    # be able to hang the (synchronous, event-loop-blocking) classify() call.
    body = {"error": {"message": "tokens " * 30_000 + "x"}}
    exc = _bad_request("Error code: 400", body=body)
    t0 = time.perf_counter()
    result = ce.classify(exc)  # must not raise regardless of match outcome
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.05, f"classify() took {elapsed:.3f}s on adversarial input"
    assert result is None or isinstance(result, ce.ContextLimitError)


def test_learn_shrinks_and_respects_manual(tmp_path):
    conn = init_db(str(tmp_path / "l.db"))
    assert mw.learn(conn, "cloud:m", 100_000) == 100_000
    assert mw.learn(conn, "cloud:m", 120_000) == 100_000        # never grows
    assert mw.learn(conn, "cloud:m", 80_000) == 80_000
    mw.upsert(conn, "cloud:m", 64_000, "manual")
    assert mw.learn(conn, "cloud:m", 30_000) == 64_000          # manual untouched
    assert mw.get(conn, "cloud:m")["source"] == "manual"


def test_learn_clamps_to_min_window_and_returns_stored_value(tmp_path):
    conn = init_db(str(tmp_path / "l2.db"))
    tiny = cc.MIN_CONTEXT_WINDOW - 1
    result = mw.learn(conn, "cloud:tiny", tiny)
    assert result == cc.MIN_CONTEXT_WINDOW                       # floored, never phantom
    assert mw.get(conn, "cloud:tiny")["window"] == result        # always what's persisted
