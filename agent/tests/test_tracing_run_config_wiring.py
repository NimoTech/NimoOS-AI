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
    raise AssertionError("no run() found")


def test_run_gates_on_flag_and_passes_run_config():
    src = _run_source()
    assert "tracing_enabled_now()" in src
    assert "build_trace_run_config(" in src
    assert "run_config=" in src
