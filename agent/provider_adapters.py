"""Provider-specific translation of unified ThinkingConfig into ModelSettings.

The frontend speaks one 4-level scale (low/medium/high/max) plus an enabled
toggle. Each provider's API exposes thinking control differently — DeepSeek
uses extra_body + reasoning_effort, OpenAI uses reasoning_effort, Anthropic
uses thinking.budget_tokens (handled in the Go service for the Anthropic
path; this module only covers OpenAI-compatible Chat Completions).
"""
import dataclasses
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from agents.model_settings import ModelSettings


class ThinkingLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    MAX = "max"


@dataclass(frozen=True)
class ThinkingConfig:
    enabled: bool
    level: ThinkingLevel


class ProviderType(str, Enum):
    DEEPSEEK = "deepseek"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    QWEN = "qwen"
    OLLAMA = "ollama"
    OPENVINO = "openvino"
    OTHER = "other"


# DeepSeek doc: low/medium silently map to "high"; xhigh maps to "max".
_DEEPSEEK_EFFORT = {
    ThinkingLevel.LOW: "high",
    ThinkingLevel.MEDIUM: "high",
    ThinkingLevel.HIGH: "max",
    ThinkingLevel.MAX: "max",
}

# OpenAI o-series / gpt-5: minimal/low/medium/high. No "max" — reuse high.
_OPENAI_EFFORT = {
    ThinkingLevel.LOW: "low",
    ThinkingLevel.MEDIUM: "medium",
    ThinkingLevel.HIGH: "high",
    ThinkingLevel.MAX: "high",
}


def build_model_settings(
    provider_type: ProviderType,
    thinking: Optional[ThinkingConfig],
) -> ModelSettings:
    """Map (provider_type, thinking) to ModelSettings for the Agents SDK.

    Anthropic is intentionally NOT handled here — Anthropic requests are
    converted in service/cloud_anthropic.go where the budget_tokens is set.
    Returning empty settings for ANTHROPIC just means we won't double-set.
    """
    settings = _thinking_settings(provider_type, thinking)
    # Always ask streaming responses to carry real token usage
    # (stream_options.include_usage). The SDK defaults this on ONLY for
    # api.openai.com; DeepSeek / Ollama / Qwen etc. support it too, and
    # without it no response ever reports real input_tokens — the context
    # indicator would be stuck on the char-ratio estimate forever. Endpoints
    # that ignore the field simply return no usage (we fall back to the
    # estimate); OpenAI-compatible servers don't reject it.
    return dataclasses.replace(settings, include_usage=True)


def _thinking_settings(
    provider_type: ProviderType,
    thinking: Optional[ThinkingConfig],
) -> ModelSettings:
    if thinking is None:
        return ModelSettings()

    if provider_type == ProviderType.DEEPSEEK:
        # parallel_tool_calls=False: DeepSeek strictly validates that every
        # assistant `tool_calls[i].id` is followed by a matching `tool` message.
        # When the Agents SDK runs parallel tools via asyncio.gather and one
        # raises, the sibling tasks get cancelled and produce no
        # function_call_output, leaving the next request with a dangling
        # tool_call → 400. Serial execution prevents the cancel cascade.
        #
        # reasoning_effort goes in extra_body (not extra_args): the SDK always
        # emits a `reasoning_effort` key in its typed kwargs (from
        # ModelSettings.reasoning) and raises TypeError on overlap with
        # extra_args. extra_body is merged into the raw JSON body, bypassing
        # both the overlap check and Pydantic's Literal validation — necessary
        # because DeepSeek's "max" effort isn't in the OpenAI Literal.
        if not thinking.enabled:
            return ModelSettings(
                parallel_tool_calls=False,
                extra_body={"thinking": {"type": "disabled"}},
            )
        return ModelSettings(
            parallel_tool_calls=False,
            extra_body={
                "thinking": {"type": "enabled"},
                "reasoning_effort": _DEEPSEEK_EFFORT[thinking.level],
            },
        )

    if provider_type == ProviderType.OPENAI:
        if not thinking.enabled:
            return ModelSettings(extra_args={"reasoning_effort": "minimal"})
        return ModelSettings(
            extra_args={"reasoning_effort": _OPENAI_EFFORT[thinking.level]},
        )

    if provider_type in (ProviderType.OLLAMA, ProviderType.QWEN, ProviderType.OPENVINO):
        # Ollama's OpenAI-compatible endpoint accepts a top-level `think` bool
        # (forwarded to /api/chat). For Qwen3 served by Ollama or DashScope this
        # toggles the <think> block emission. `chat_template_kwargs.enable_thinking`
        # is sent alongside as a fallback for backends that read template kwargs
        # (vLLM, SGLang, recent Ollama builds) instead of the bespoke `think` field.
        return ModelSettings(
            extra_body={
                "think": bool(thinking.enabled),
                "chat_template_kwargs": {"enable_thinking": bool(thinking.enabled)},
            },
        )

    # ANTHROPIC handled in Go service. OTHER: pass through.
    return ModelSettings()


_VISION_KNOWN_FALSE = {
    # Confirmed text-only families. Anything not in this list is assumed
    # vision-capable; if the model actually rejects the image, the provider
    # surfaces a 4xx that the user sees directly. That tradeoff is better
    # than silently degrading to text and having the model claim it cannot
    # see attachments the user clearly attached.
    "deepseek": lambda m: True,
}


def model_supports_vision(provider_type: str, model_name: str) -> bool:
    """Best-effort capability check. Default True for unknown (provider/model);
    only return False for families we know are text-only."""
    if not model_name:
        return False
    pt = (provider_type or "other").lower()
    if pt in _VISION_KNOWN_FALSE:
        return not _VISION_KNOWN_FALSE[pt](model_name)
    return True
