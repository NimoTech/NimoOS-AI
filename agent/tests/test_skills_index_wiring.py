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
    raise AssertionError("could not find a class with a run() method")


def test_run_injects_skill_index_guarded_by_profile():
    src = _run_source()
    assert "skills_registry.render_index_block()" in src
    # The injection guard must appear before the call site.
    assert src.index("profile.tools is None") < src.index(
        "skills_registry.render_index_block()")
