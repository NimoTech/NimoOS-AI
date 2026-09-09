def test_budget_for_window_caps_by_window(monkeypatch):
    from ask import config
    assert config.budget_for_window(None) == config.EVIDENCE_BUDGET_CHARS
    assert config.budget_for_window(32768) == config.EVIDENCE_BUDGET_CHARS   # 32k*1.2 > 24000
    assert config.budget_for_window(8192) == int(8192 * 1.2)


def test_pipeline_enabled_env_switch(monkeypatch):
    from ask import config
    monkeypatch.delenv("NIMOOS_ASK_PIPELINE", raising=False)
    assert config.pipeline_enabled() is True
    monkeypatch.setenv("NIMOOS_ASK_PIPELINE", "0")
    assert config.pipeline_enabled() is False


def test_step_summary_mode_defaults_auto(monkeypatch):
    from ask import config
    monkeypatch.delenv("NIMOOS_ASK_STEP_SUMMARY", raising=False)
    assert config.step_summary_mode() == "auto"
    monkeypatch.setenv("NIMOOS_ASK_STEP_SUMMARY", "bogus")
    assert config.step_summary_mode() == "auto"
    monkeypatch.setenv("NIMOOS_ASK_STEP_SUMMARY", "off")
    assert config.step_summary_mode() == "off"


def test_prompt_carries_answer_contract():
    from ask import prompt
    p = prompt.ASK_SYSTEM_PROMPT
    assert "[n]" in p and "Sources" in p and "[EVIDENCE START]" in p
    assert "at most 3" in p.lower() or "at most three" in p.lower()
    assert "needs_retrieval" in prompt.REWRITE_INSTRUCTION
    assert "queries" in prompt.REWRITE_INSTRUCTION
