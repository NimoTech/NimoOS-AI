"""Rolling-summary LLM call for compaction (spec §5.4).

Prefers the user's background_model (the small/cheap model already used for
notes distillation) so a mid-run fold does not go through the possibly
rate-limited session provider; falls back to the session client. Auxiliary
one-shot calls on either path (background client or session fallback) get
thinking disabled via aux_thinking_kwargs — a thinking model burns thousands
of reasoning tokens on a short summary/rewrite prompt and times out.
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

from openai import AsyncOpenAI

from context_compaction import COMPACT_LLM_TIMEOUT  # noqa: F401 — monkeypatched in tests

_LOG = logging.getLogger("nimoos-agent.compaction")

# Hostnames (as base_url suffixes) of "other"-type endpoints verified live to
# think by default and to honor an OpenAI-style extra_body override.
_OTHER_THINKING_HOSTS = ("volces.com",)


def _new_client(base_url: str, api_key: str):
    return AsyncOpenAI(base_url=base_url, api_key=api_key or "none", max_retries=0)


def aux_thinking_kwargs(provider_type: str, base_url: str = "") -> dict:
    """Extra chat-completion kwargs that disable thinking for auxiliary
    one-shot calls (background summarizer / rewrite fallback). Shared by
    resolve_background_client and make_summarizer's session-fallback branch
    so both paths follow one rule. Never raises — worst case is thinking
    stays on and the caller's own timeout still protects it."""
    try:
        if provider_type in ("deepseek", "qwen", "ollama"):
            from provider_adapters import (  # noqa: PLC0415
                ProviderType, ThinkingConfig, ThinkingLevel, build_model_settings,
            )
            settings = build_model_settings(
                ProviderType(provider_type), ThinkingConfig(enabled=False, level=ThinkingLevel.LOW))
            extra: dict = {}
            if getattr(settings, "extra_body", None):
                extra["extra_body"] = settings.extra_body
            if getattr(settings, "extra_args", None):
                extra["extra_args"] = settings.extra_args
            return extra
        if provider_type == "other":
            host = urlparse(base_url or "").hostname or ""
            if host.endswith(_OTHER_THINKING_HOSTS):
                return {"extra_body": {"thinking": {"type": "disabled"}}}
            return {}
        return {}
    except Exception as exc:  # noqa: BLE001 — never raise from a thinking-control helper
        _LOG.info("aux_thinking_kwargs(%s) failed: %s", provider_type, exc)
        return {}


def session_complete_fn(client, model_name, extra_kwargs=None):
    """Generic one-shot completion (system + user -> text) on `client`."""
    async def _complete(instruction: str, body: str, *, max_tokens: int = 1024) -> str:
        resp = await client.chat.completions.create(
            model=model_name,
            messages=[{"role": "system", "content": instruction},
                      {"role": "user", "content": body}],
            temperature=0.3, max_tokens=max_tokens, **(extra_kwargs or {}))
        if resp.choices:
            return (getattr(resp.choices[0].message, "content", "") or "").strip()
        return ""
    return _complete


def session_summarize_fn(client, model_name, extra_kwargs=None):
    complete = session_complete_fn(client, model_name, extra_kwargs)

    async def _summarize(instruction: str, prior_summary: str, fold_text: str) -> str:
        body = (f"[Existing summary]\n{prior_summary or '(none)'}\n\n"
                f"[Earlier conversation excerpts]\n{fold_text}")
        return await complete(instruction, body)
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
    extra = aux_thinking_kwargs(creds.get("provider_type", ""), creds.get("base_url", ""))
    return _new_client(creds["base_url"], creds.get("api_key", "")), creds["model"], extra


def _summarize_via(complete):
    async def _summarize(instruction: str, prior_summary: str, fold_text: str) -> str:
        body = (f"[Existing summary]\n{prior_summary or '(none)'}\n\n"
                f"[Earlier conversation excerpts]\n{fold_text}")
        return await complete(instruction, body)
    return _summarize


def make_summarizer(conn, user_id: str, session_client, model_name: str, *,
                     creds_resolver=None, provider_type: str = "other", base_url: str = ""):
    state: dict = {"resolved": False, "bg": None, "fn": None, "complete": None}

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
            state["complete"] = session_complete_fn(client, model, extra)
        else:
            state["complete"] = session_complete_fn(
                session_client, model_name, aux_thinking_kwargs(provider_type, base_url))
        state["fn"] = _summarize_via(state["complete"])
        return state["fn"]

    async def summarize(instruction: str, prior: str, fold: str) -> str:
        try:
            fn = await _pick()
            timeout = globals()["COMPACT_LLM_TIMEOUT"]
            return await asyncio.wait_for(fn(instruction, prior, fold), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("compaction summarize failed: %s %r", type(exc).__name__, exc)
            return ""

    async def complete(instruction: str, body: str, *, max_tokens: int = 1024,
                       timeout: float | None = None) -> str:
        """One-shot completion on the same (background-preferred) client;
        used by offload summaries. Returns "" on any failure."""
        try:
            await _pick()
            fn = state["complete"]
            timeout = timeout if timeout is not None else globals()["COMPACT_LLM_TIMEOUT"]
            return await asyncio.wait_for(fn(instruction, body, max_tokens=max_tokens), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("compaction complete() failed: %s %r", type(exc).__name__, exc)
            return ""

    async def aclose():
        bg = state.get("bg")
        if bg is not None:
            try:
                await bg.close()
            except Exception:  # noqa: BLE001
                pass

    summarize.aclose = aclose  # type: ignore[attr-defined]
    summarize.complete = complete  # type: ignore[attr-defined]
    return summarize
