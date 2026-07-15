"""Local Ollama judge for GRAY shell commands. Mirrors egress/judge.py.

judge_command(command) -> "allow" | "ask"
Fail-safe: ANY error returns "ask".
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

_DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
_DEFAULT_MODEL = "qwen3.5:0.8b"
_DEFAULT_TIMEOUT = 20.0
_VALID = {"allow", "ask"}


def _ollama_url() -> str:
    return os.environ.get("NIMOOS_OLLAMA_URL", _DEFAULT_OLLAMA_URL).rstrip("/")


def _model() -> str:
    return os.environ.get("NIMOOS_SHELL_JUDGE_MODEL",
                          os.environ.get("NIMOOS_EGRESS_JUDGE_MODEL", _DEFAULT_MODEL))


def _timeout() -> float:
    try:
        return float(os.environ.get("NIMOOS_SHELL_JUDGE_TIMEOUT", _DEFAULT_TIMEOUT))
    except (ValueError, TypeError):
        return _DEFAULT_TIMEOUT


def _build_prompt(command: str, model: str) -> str:
    no_think = "/no_think" if "qwen3" in model.lower() else ""
    return (
        f"{no_think}\n"
        "You are a security classifier for shell commands about to run as root "
        "on a home NAS with the user's data mounted.\n"
        "Decide whether the command is safe to run WITHOUT human confirmation.\n"
        "Treat as UNSAFE (ask): deleting/overwriting user files, modifying system "
        "state, downloading+executing code, or anything whose effect you cannot "
        "confidently predict.\n"
        "Respond ONLY with JSON (no markdown):\n"
        '{"verdict":"<allow|ask>","reason":"<brief>"}\n'
        "  allow — clearly harmless / read-only-ish, safe to auto-run\n"
        "  ask   — anything destructive or uncertain; needs human review\n\n"
        f"Command:\n{command}"
    )


def _call_ollama_sync(url: str, model: str, prompt: str, timeout: float) -> dict:
    payload = json.dumps({
        "model": model, "prompt": prompt,
        "stream": False, "format": "json", "think": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Ollama HTTP {resp.status}")
        return json.loads(resp.read())


async def judge_command(command: str) -> str:
    model = _model()
    url = f"{_ollama_url()}/api/generate"
    prompt = _build_prompt(command, model)
    fn = partial(_call_ollama_sync, url, model, prompt, _timeout())
    loop = asyncio.get_running_loop()
    try:
        resp = await loop.run_in_executor(None, fn)
    except Exception as exc:  # noqa: BLE001 — fail-safe to ask
        logger.warning("shell_guard.judge: ollama error: %s", exc)
        return "ask"
    raw = resp.get("response", "")
    if not isinstance(raw, str) or not raw.strip():
        thinking = resp.get("thinking")
        raw = thinking if isinstance(thinking, str) and thinking.strip() else ""
    if not raw:
        return "ask"
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        return "ask"
    verdict = out.get("verdict", "")
    return verdict if verdict in _VALID else "ask"
