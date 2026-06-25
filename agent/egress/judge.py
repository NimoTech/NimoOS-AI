"""
egress/judge.py — Local Ollama privacy judge.

async judge(content: bytes, host: str) -> str
    Asks a local Ollama model whether *content* (truncated to NIMOOS_EGRESS_JUDGE_MAXBYTES)
    is safe to send to *host*.

Returns one of: "allow" | "block" | "ask"

Fail-safe: ANY error (connection, timeout, HTTP non-200, bad JSON, unknown verdict)
returns "ask" — never silently allow on failure.

Environment variables:
    NIMOOS_OLLAMA_URL           default: http://127.0.0.1:11434
    NIMOOS_EGRESS_JUDGE_MODEL   default: qwen3.5:0.8b
    NIMOOS_EGRESS_JUDGE_TIMEOUT default: 20.0  (seconds)
    NIMOOS_EGRESS_JUDGE_MAXBYTES default: 4096
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from functools import partial

logger = logging.getLogger("nimoos-agent")

# ─── Config helpers ───────────────────────────────────────────────────────────

_DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
_DEFAULT_MODEL = "qwen3.5:0.8b"
_DEFAULT_TIMEOUT = 20.0
_DEFAULT_MAXBYTES = 4096

_VALID_VERDICTS = {"allow", "block", "ask"}


def _ollama_url() -> str:
    return os.environ.get("NIMOOS_OLLAMA_URL", _DEFAULT_OLLAMA_URL).rstrip("/")


def _model() -> str:
    return os.environ.get("NIMOOS_EGRESS_JUDGE_MODEL", _DEFAULT_MODEL)


def _timeout() -> float:
    try:
        return float(os.environ.get("NIMOOS_EGRESS_JUDGE_TIMEOUT", _DEFAULT_TIMEOUT))
    except (ValueError, TypeError):
        return _DEFAULT_TIMEOUT


def _maxbytes() -> int:
    try:
        return int(os.environ.get("NIMOOS_EGRESS_JUDGE_MAXBYTES", _DEFAULT_MAXBYTES))
    except (ValueError, TypeError):
        return _DEFAULT_MAXBYTES


# ─── Prompt builder ───────────────────────────────────────────────────────────

def _build_prompt(content_text: str, host: str, model: str) -> str:
    """
    Build the Ollama prompt. The model is instructed to output only JSON.
    We embed host and truncated content, then demand a strict verdict.
    """
    # For qwen3 thinking models, add /no_think to disable chain-of-thought
    no_think = "/no_think" if "qwen3" in model.lower() else ""
    return (
        f"{no_think}\n"
        "You are a privacy and data-loss-prevention (DLP) classifier.\n"
        f"The following content is about to be sent over the network to host: {host!r}\n\n"
        "Analyse whether the content contains any of the following:\n"
        "  - Private keys, certificates, or cryptographic secrets\n"
        "  - API keys, tokens, or credentials\n"
        "  - Personally identifiable information (PII): names, emails, phone numbers, ID numbers\n"
        "  - Passwords or passphrases\n"
        "  - Any other sensitive private information\n\n"
        "Respond ONLY with valid JSON in this exact format (no markdown, no extra text):\n"
        '{"verdict": "<allow|block|ask>", "reason": "<brief reason>"}\n\n'
        "  allow — content is safe to transmit\n"
        "  block — content clearly contains sensitive/private data; must not be sent\n"
        "  ask   — uncertain; human review needed\n\n"
        f"Content to analyse:\n{content_text}"
    )


# ─── Synchronous HTTP call (runs in executor) ─────────────────────────────────

def _call_ollama_sync(
    url: str,
    model: str,
    prompt: str,
    timeout: float,
) -> dict:
    """
    POST to Ollama /api/generate and return the parsed JSON response dict.

    Raises on any error so the async wrapper can catch and fail-safe to "ask".
    """
    payload = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Ollama returned HTTP {resp.status}")
        body = resp.read()

    return json.loads(body)


# ─── Public async interface ───────────────────────────────────────────────────

async def judge(content: bytes, host: str) -> str:
    """
    Ask the local Ollama model whether *content* is safe to send to *host*.

    Returns "allow", "block", or "ask".
    Any failure (network, timeout, bad JSON, unknown verdict) returns "ask".
    """
    maxbytes = _maxbytes()
    model = _model()
    timeout = _timeout()
    url = f"{_ollama_url()}/api/generate"

    # Truncate bytes then decode, replacing undecodable bytes
    content_text = content[:maxbytes].decode("utf-8", errors="replace")
    prompt = _build_prompt(content_text, host, model)

    loop = asyncio.get_running_loop()
    fn = partial(_call_ollama_sync, url, model, prompt, timeout)

    try:
        response_dict = await loop.run_in_executor(None, fn)
    except urllib.error.URLError as exc:
        logger.warning("egress.judge: connection error reaching Ollama: %s", exc)
        return "ask"
    except TimeoutError as exc:
        logger.warning("egress.judge: timeout calling Ollama: %s", exc)
        return "ask"
    except Exception as exc:  # noqa: BLE001
        logger.warning("egress.judge: unexpected error calling Ollama: %s", exc)
        return "ask"

    # Ollama wraps the model output in the "response" field
    raw_response = response_dict.get("response", "")
    if not isinstance(raw_response, str):
        logger.warning(
            "egress.judge: Ollama 'response' field is not a string: %r", raw_response
        )
        return "ask"

    try:
        model_output = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        logger.warning(
            "egress.judge: model output is not valid JSON (%s): %r", exc, raw_response[:200]
        )
        return "ask"

    verdict = model_output.get("verdict", "")
    if verdict not in _VALID_VERDICTS:
        logger.warning(
            "egress.judge: unknown verdict %r from model (reason: %r)",
            verdict,
            model_output.get("reason", ""),
        )
        return "ask"

    return verdict
