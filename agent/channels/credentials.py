"""Resolve per-user LLM provider credentials via the Go service's
localhost-only internal endpoint. Cloud provider keys are encrypted at rest
with the master key that only the Go layer holds; this is the sanctioned
way for in-process channel consumers to obtain them."""
from __future__ import annotations

import os

import httpx

_DEFAULT_RUNTIME_PATH = "/var/run/nimoos"
_ENDPOINT = "/v1/ai/_internal/agent/provider-credentials"


def _internal_base() -> str | None:
    env = os.environ.get("NIMOOS_AI_INTERNAL_URL")
    if env:
        return env.rstrip("/")
    path = os.path.join(
        os.environ.get("NIMOOS_RUNTIME_PATH", _DEFAULT_RUNTIME_PATH), "ai.url")
    try:
        with open(path) as f:
            base = f.read().strip()
    except OSError:
        return None
    return base.rstrip("/") or None


async def resolve(user_id: str, model: str, *,
                  transport: httpx.AsyncBaseTransport | None = None) -> dict | None:
    base = _internal_base()
    if not base:
        return None
    try:
        async with httpx.AsyncClient(transport=transport, timeout=10) as client:
            r = await client.get(base + _ENDPOINT,
                                 params={"user_id": user_id, "model": model})
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("base_url") or not data.get("model"):
        return None
    return data
