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
    i_index = src.index("skills_registry.render_index_block(_rt_view)")
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


def test_forced_call_falls_back_once_when_nothing_was_produced():
    src = _run_source()
    i_runner = src.index("Runner.run_streamed(")
    i_fallback = src.index('"type": "skill_activation_fallback"')
    i_final = src.index('final = getattr(stream, "final_output", None)')
    # The fallback decision sits after the streaming loop and before the
    # reasoning-only fallback that reads stream.final_output.
    assert i_runner < i_fallback < i_final
    assert "tool_choice=None" in src
    assert "forced_retry_done" in src
    assert "skill_activation.should_retry_without_pin(" in src


def test_runtime_view_scan_is_inside_an_exception_boundary_and_shared():
    src = _run_source()
    i_scan = src.index("_rt_view = skills_registry._scan_runtime_view()")
    assert src[max(0, i_scan - 200):i_scan].rstrip().endswith("try:")
    assert "render_index_block(_rt_view)" in src
    assert "select_auto_skill(\n                    message, _rt_view)" in src or "select_auto_skill(message, _rt_view)" in src


def test_pin_requires_pin_flag_and_injected_body():
    src = _run_source()
    i = src.index("forced_tool = activated.first_tool")
    cond = src[max(0, i - 400):i]
    assert "activated.pin" in cond and "activation_injected" in cond


def test_retry_uses_the_predicate():
    src = _run_source()
    assert "skill_activation.should_retry_without_pin(" in src
    assert "not message_emitted and not call_names" not in src


def test_fallback_retry_nudges_the_model():
    src = _run_source()
    i_release = src.index("tool_choice=None)")
    i_nudge = src.index("Retry notice: your first attempt returned no tool call")
    i_continue = src.index("continue", i_release)
    assert i_release < i_nudge < i_continue
