import dataclasses
import inspect

from agents import ModelSettings

import agent as agentmod


def _run_source():
    for name in dir(agentmod):
        obj = getattr(agentmod, name)
        if isinstance(obj, type) and hasattr(obj, "run"):
            try:
                return inspect.getsource(obj.run)
            except (OSError, TypeError):
                continue
    raise AssertionError("could not find a class with a run() method")


def test_activation_runs_after_index_and_before_agent_construction():
    src = _run_source()
    i_index = src.index("skills_registry.render_index_block()")
    i_select = src.index("skill_activation.select_auto_skill(")
    i_agent = src.index("agent = Agent(")
    assert i_index < i_select < i_agent


def test_activation_is_guarded_to_general_chat_turns():
    src = _run_source()
    i_select = src.index("skill_activation.select_auto_skill(")
    guard = src[max(0, i_select - 400):i_select]
    assert "profile.tools is None" in guard
    assert 'kind == "chat"' in guard
    assert "not continue_run" in guard


def test_tool_choice_is_pinned_only_when_forced_tool_present():
    src = _run_source()
    i_tools = src.index("run_tools = select_tools_for_run(")
    i_pin = src.index("tool_choice=forced_tool")
    i_agent = src.index("agent = Agent(")
    assert i_tools < i_pin < i_agent
    assert "skill_activation.forcing_enabled()" in src
    assert "skill_activation.FORCE_PROVIDER_TYPES" in src


def test_skill_activated_event_precedes_runner():
    src = _run_source()
    assert src.index('"type": "skill_activated"') < src.index("Runner.run_streamed(")


def test_model_settings_accepts_named_tool_choice():
    # The SDK contract the pin relies on (agents 0.19.3): a bare tool name is a
    # valid tool_choice and Agent.reset_tool_choice defaults to True.
    ms = dataclasses.replace(ModelSettings(), tool_choice="nimoos_search")
    assert ms.tool_choice == "nimoos_search"
    from agents import Agent
    assert inspect.signature(Agent).parameters["reset_tool_choice"].default is True
