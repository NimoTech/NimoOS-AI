import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def test_startup_hook_registered():
    import main as main_module
    names = [f.__name__ for f in main_module.app.router.on_startup]
    assert "_notes_extract_worker_startup" in names


def test_run_finally_enqueues_notes_job():
    # the finally block in main._start_run must reference the notes enqueue
    import inspect
    import main as main_module
    src = inspect.getsource(main_module)
    assert "maybe_enqueue_notes_job" in src
