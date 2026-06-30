import inspect
import agent as agentmod


def _run_source():
    for name in dir(agentmod):
        obj = getattr(agentmod, name)
        if isinstance(obj, type) and hasattr(obj, "run"):
            try:
                return inspect.getsource(obj.run)
            except (OSError, TypeError):
                continue
    raise AssertionError("could not find a class with a run() method in agent.py")


def test_run_passes_trace_run_config():
    src = _run_source()
    assert "build_trace_run_config(" in src, "run() should build a trace RunConfig"
    assert "run_config=" in src, "run_streamed should receive run_config="


def test_agent_module_imports_phoenix_tracing():
    assert "phoenix_tracing" in inspect.getsource(agentmod)
