import inspect
import agent as agentmod
from skills import shell  # noqa: F401  (ensures module imports cleanly)


def _run_source():
    # Locate the class that defines run() and return its source.
    for name in dir(agentmod):
        obj = getattr(agentmod, name)
        if isinstance(obj, type) and hasattr(obj, "run"):
            try:
                return inspect.getsource(obj.run)
            except (OSError, TypeError):
                continue
    raise AssertionError("could not find a class with a run() method in agent.py")


def test_run_sets_shell_db_and_patterns_vars():
    src = _run_source()
    for name in ("shell_skills.DB_VAR.set",
                 "shell_skills.USER_PATTERNS_VAR.set",
                 "shell_skills.CONFIRM_MGR_VAR.set",
                 "shell_skills.EVENT_QUEUE_VAR.set"):
        assert name in src, f"missing wiring: {name}"


def test_system_prompt_mentions_shell_readonly_and_network():
    assert "read-only" in agentmod.SYSTEM_PROMPT
    assert "network=true" in agentmod.SYSTEM_PROMPT
