import pytest
from provider_adapters import (
    ThinkingLevel, ThinkingConfig, build_model_settings, ProviderType,
)


def test_deepseek_disabled():
    s = build_model_settings(
        ProviderType.DEEPSEEK,
        ThinkingConfig(enabled=False, level=ThinkingLevel.MEDIUM),
    )
    # DeepSeek expects extra_body.thinking.type == "disabled"
    assert s.extra_body == {"thinking": {"type": "disabled"}}
    # No reasoning_effort when disabled
    assert "reasoning_effort" not in (s.extra_args or {})


@pytest.mark.parametrize("level,expected_effort", [
    (ThinkingLevel.LOW,    "high"),
    (ThinkingLevel.MEDIUM, "high"),
    (ThinkingLevel.HIGH,   "max"),
    (ThinkingLevel.MAX,    "max"),
])
def test_deepseek_levels(level, expected_effort):
    s = build_model_settings(
        ProviderType.DEEPSEEK,
        ThinkingConfig(enabled=True, level=level),
    )
    assert s.extra_body == {"thinking": {"type": "enabled"}}
    assert s.extra_args["reasoning_effort"] == expected_effort


@pytest.mark.parametrize("level,expected_effort", [
    (ThinkingLevel.LOW,    "low"),
    (ThinkingLevel.MEDIUM, "medium"),
    (ThinkingLevel.HIGH,   "high"),
    (ThinkingLevel.MAX,    "high"),  # OpenAI has no max → reuse high
])
def test_openai_levels(level, expected_effort):
    s = build_model_settings(
        ProviderType.OPENAI,
        ThinkingConfig(enabled=True, level=level),
    )
    assert s.extra_args["reasoning_effort"] == expected_effort


def test_openai_disabled_uses_minimal():
    s = build_model_settings(
        ProviderType.OPENAI,
        ThinkingConfig(enabled=False, level=ThinkingLevel.MEDIUM),
    )
    assert s.extra_args["reasoning_effort"] == "minimal"


def test_other_provider_returns_empty_settings():
    """Unknown provider types skip thinking params entirely."""
    s = build_model_settings(
        ProviderType.OTHER,
        ThinkingConfig(enabled=True, level=ThinkingLevel.HIGH),
    )
    assert (s.extra_args or {}) == {}
    assert (s.extra_body or {}) == {}


def test_none_thinking_returns_empty_settings():
    s = build_model_settings(ProviderType.DEEPSEEK, None)
    assert (s.extra_args or {}) == {}
    assert (s.extra_body or {}) == {}
