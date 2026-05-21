"""LLM call to /v1/ai/chat/completions + three-defense JSON parsing.

Per spec §5.2, this module MUST only ever raise LLMError or JSONParseError.
Bare httpx.HTTPError or ValueError must NOT leak — the worker main loop
classifies LLMError/JSONParseError as per-node failure (writes failed
placeholder summary), while anything else is treated as transient round
failure (break out of the loop, retry next timer).

The X-NimoOS-User-ID header is resolved at call time via
discovery.resolve_user_id(cfg). See discovery.resolve_user_id docstring
for the resolution order.
"""
from __future__ import annotations
import json
import re
import httpx

from wiki_summary_worker import discovery, prompt
from wiki_summary_worker.config import Config
from wiki_summary_worker.sampler import Evidence


class LLMError(Exception):
    """LLM HTTP / network / response-shape failure. Per-node level."""


class JSONParseError(Exception):
    """LLM output didn't yield a valid {ai_label, summary} object. Per-node."""


def _make_client(timeout: int = 60) -> httpx.Client:
    return httpx.Client(timeout=timeout)


def summarize(evidence: Evidence, cfg: Config) -> dict:
    body = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": prompt.SYSTEM},
            {"role": "user", "content": prompt.serialize_user_message(evidence)},
        ],
        "temperature": 0.2,
        "max_tokens": 400,
        "response_format": {"type": "json_object"},  # defense 1
    }
    headers = {"X-NimoOS-User-ID": discovery.resolve_user_id(cfg)}

    try:
        with _make_client(cfg.llm_timeout_sec) as c:
            r = c.post(
                discovery.ai_url() + "/v1/ai/chat/completions",
                json=body, headers=headers,
            )
            r.raise_for_status()
    except httpx.HTTPError as e:
        raise LLMError(f"chat-completions failed: {e}") from e

    try:
        raw = r.json()["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, json.JSONDecodeError, TypeError) as e:
        raise LLMError(f"unexpected chat-completions response shape: {e}") from e

    # defense 2: strip markdown code fences
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    # defense 3: regex first {…} object
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            raise JSONParseError(f"no JSON object in LLM output: {raw[:200]}")
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise JSONParseError(f"regex'd object still invalid: {e}") from e

    if not isinstance(parsed, dict):
        raise JSONParseError(f"LLM returned non-object JSON: {type(parsed).__name__}")
    if "ai_label" not in parsed or "summary" not in parsed:
        raise JSONParseError(f"missing required fields: {sorted(parsed.keys())}")

    return {
        "ai_label": str(parsed["ai_label"])[:80],
        "summary":  str(parsed["summary"])[:600],
    }
