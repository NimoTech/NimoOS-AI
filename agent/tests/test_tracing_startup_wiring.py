import inspect
import main as mainmod


def test_main_imports_setup_tracing():
    src = inspect.getsource(mainmod)
    assert "phoenix_tracing" in src, "main.py should import the tracing module"
    assert "setup_tracing()" in src, "main.py should call setup_tracing() at startup"


def test_setup_tracing_is_in_a_startup_handler():
    # The call must live inside an on_event('startup') handler so it runs once at boot.
    src = inspect.getsource(mainmod)
    assert 'on_event("startup")' in src or "on_event('startup')" in src
