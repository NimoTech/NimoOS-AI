"""build_trace_run_config's RunConfig-fallback rungs (Task 6 review fix #1).

phoenix_tracing.build_trace_run_config does `from agents import RunConfig`
locally inside the function, so a fake class must be installed as
agents.RunConfig (monkeypatching the module attribute agents looks up at
call time) rather than the name imported into phoenix_tracing's namespace.
"""
import agents
import phoenix_tracing


class _FakeConfigBase:
    def __init__(self, **kw):
        self.kw = kw
        for k, v in kw.items():
            setattr(self, k, v)


def test_rejects_filter_keeps_tool_error_kwargs(monkeypatch):
    """An SDK that rejects call_model_input_filter but accepts the
    tool-error kwargs must still get tool_not_found_behavior — the filter
    alone is dropped, not everything."""
    class FakeRunConfig(_FakeConfigBase):
        def __init__(self, **kw):
            if "call_model_input_filter" in kw:
                raise TypeError("call_model_input_filter not supported")
            super().__init__(**kw)

    monkeypatch.setattr(agents, "RunConfig", FakeRunConfig)
    cfg = phoenix_tracing.build_trace_run_config(
        False, "s", "u", "m", "chat", call_model_input_filter=object())
    assert cfg.tool_not_found_behavior == "return_error_to_model"
    assert not hasattr(cfg, "call_model_input_filter")


def test_rejects_tool_error_kwargs_keeps_filter(monkeypatch):
    """The host-SDK case observed in this repo: tool_not_found_behavior is
    rejected but call_model_input_filter is supported — the filter must
    survive even though the tool-error kwargs get dropped."""
    sentinel = object()

    class FakeRunConfig(_FakeConfigBase):
        def __init__(self, **kw):
            if "tool_not_found_behavior" in kw or "tool_error_formatter" in kw:
                raise TypeError("tool-error kwargs not supported")
            super().__init__(**kw)

    monkeypatch.setattr(agents, "RunConfig", FakeRunConfig)
    cfg = phoenix_tracing.build_trace_run_config(
        False, "s", "u", "m", "chat", call_model_input_filter=sentinel)
    assert cfg.call_model_input_filter is sentinel
    assert not hasattr(cfg, "tool_not_found_behavior")
    assert not hasattr(cfg, "tool_error_formatter")


def test_accepts_everything(monkeypatch):
    """An SDK that supports both keeps both on the first attempt."""
    sentinel = object()
    monkeypatch.setattr(agents, "RunConfig", _FakeConfigBase)
    cfg = phoenix_tracing.build_trace_run_config(
        False, "s", "u", "m", "chat", call_model_input_filter=sentinel)
    assert cfg.call_model_input_filter is sentinel
    assert cfg.tool_not_found_behavior == "return_error_to_model"
