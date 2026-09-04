"""Rolling-summary LLM call for compaction (spec §5.4).

Prefers the user's background_model (the small/cheap model already used for
notes distillation) so a mid-run fold does not go through the possibly
rate-limited session provider; falls back to the session client. Ollama/qwen
backgrounds get thinking disabled exactly like notes_distill does — a thinking
model burns thousands of reasoning tokens on a summary and times out.
"""
from __future__ import annotations

import asyncio
import logging

from openai import AsyncOpenAI

from context_compaction import COMPACT_LLM_TIMEOUT  # noqa: F401 — monkeypatched in tests

_LOG = logging.getLogger("nimoos-agent.compaction")


def _new_client(base_url: str, api_key: str):
    return AsyncOpenAI(base_url=base_url, api_key=api_key or "none", max_retries=0)


def session_summarize_fn(client, model_name, extra_kwargs=None):
    async def _summarize(instruction: str, prior_summary: str, fold_text: str) -> str:
        body = (f"[Existing summary]\n{prior_summary or '(none)'}\n\n"
                f"[Earlier conversation excerpts]\n{fold_text}")
        resp = await client.chat.completions.create(
            model=model_name,
            messages=[{"role": "system", "content": instruction},
                      {"role": "user", "content": body}],
            temperature=0.3, max_tokens=1024, **(extra_kwargs or {}))
        if resp.choices:
            return (getattr(resp.choices[0].message, "content", "") or "").strip()
        return ""
    return _summarize


async def _default_creds(user_id: str, model: str):
    from channels import credentials  # noqa: PLC0415
    return await credentials.resolve(user_id, model)


async def resolve_background_client(conn, user_id: str, *, creds_resolver=None):
    """(client, model, extra_kwargs) for the user's background_model, or None."""
    from notes import store as notes_store  # noqa: PLC0415
    model = notes_store.get_background_model(conn, user_id)
    if not model:
        return None
    creds = await (creds_resolver or _default_creds)(user_id, model)
    if not creds or not creds.get("base_url") or not creds.get("model"):
        return None
    extra: dict = {}
    if creds.get("provider_type") in ("ollama", "qwen"):
        from provider_adapters import ProviderType, ThinkingConfig, ThinkingLevel, build_model_settings  # noqa: PLC0415
        settings = build_model_settings(ProviderType.OLLAMA, ThinkingConfig(enabled=False, level=ThinkingLevel.LOW))
        if getattr(settings, "extra_body", None):
            extra["extra_body"] = settings.extra_body
    return _new_client(creds["base_url"], creds.get("api_key", "")), creds["model"], extra


def make_summarizer(conn, user_id: str, session_client, model_name: str, *, creds_resolver=None):
    state: dict = {"resolved": False, "bg": None, "fn": None}

    async def _pick():
        if state["resolved"]:
            return state["fn"]
        state["resolved"] = True
        try:
            bg = await asyncio.wait_for(
                resolve_background_client(conn, user_id, creds_resolver=creds_resolver), timeout=10)
        except Exception as exc:  # noqa: BLE001 — fall back, never raise
            _LOG.info("compaction: background model unavailable (%s); using session model", exc)
            bg = None
        if bg:
            client, model, extra = bg
            state["bg"] = client
            state["fn"] = session_summarize_fn(client, model, extra)
        else:
            state["fn"] = session_summarize_fn(session_client, model_name)
        return state["fn"]

    async def summarize(instruction: str, prior: str, fold: str) -> str:
        try:
            fn = await _pick()
            timeout = globals()["COMPACT_LLM_TIMEOUT"]
            return await asyncio.wait_for(fn(instruction, prior, fold), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("compaction summarize failed: %s", exc)
            return ""

    async def aclose():
        bg = state.get("bg")
        if bg is not None:
            try:
                await bg.close()
            except Exception:  # noqa: BLE001
                pass

    summarize.aclose = aclose  # type: ignore[attr-defined]
    return summarize
