"""Provider-specific translation of unified ThinkingConfig into ModelSettings.

The frontend speaks one 4-level scale (low/medium/high/max) plus an enabled
toggle. Each provider's API exposes thinking control differently — DeepSeek
uses extra_body + reasoning_effort, OpenAI uses reasoning_effort, Anthropic
uses thinking.budget_tokens (handled in the Go service for the Anthropic
path; this module only covers OpenAI-compatible Chat Completions).
"""
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
    if thinking is None:
        return ModelSettings()

    if provider_type == ProviderType.DEEPSEEK:
        if not thinking.enabled:
            return ModelSettings(
                extra_body={"thinking": {"type": "disabled"}},
            )
        return ModelSettings(
            extra_body={"thinking": {"type": "enabled"}},
            extra_args={"reasoning_effort": _DEEPSEEK_EFFORT[thinking.level]},
        )

    if provider_type == ProviderType.OPENAI:
        if not thinking.enabled:
            return ModelSettings(extra_args={"reasoning_effort": "minimal"})
        return ModelSettings(
            extra_args={"reasoning_effort": _OPENAI_EFFORT[thinking.level]},
        )

    # ANTHROPIC handled in Go service. QWEN/OLLAMA/OTHER: pass through.
    return ModelSettings()
