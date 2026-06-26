import main as main_module
import memory_extract as mx
import db as db_module
import memory_store as ms


def test_default_history_loader_reads_latest_messages(tmp_path):
    import json as _j
    import importlib
    import sys
    # Re-import db freshly so we get whichever db module object is currently
    # live in sys.modules — the same one memory_extract._default_history_loader
    # will resolve when it calls `import db` at runtime.
    if "db" in sys.modules:
        del sys.modules["db"]
    import db as _db
    conn = _db.init_db(str(tmp_path / "agent.db"))  # publishes db._conn singleton
    conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) "
                 "VALUES('s1','u1',0,0)")
    conn.execute("INSERT INTO messages(id,session_id,role,content,created_at) "
                 "VALUES('m1','s1','user',?,1)",
                 (_j.dumps([{"role": "user", "content": "hi"}]),))
    conn.commit()
    hist = mx._default_history_loader("s1")
    assert hist == [{"role": "user", "content": "hi"}]


def test_main_registers_memory_worker_startup():
    # the startup hook that launches the worker must be registered
    names = [h.__name__ for h in main_module.app.router.on_startup]
    assert "_memory_worker_startup" in names


def test_enqueue_helper_used_on_run_end(tmp_path, monkeypatch):
    # The finally-block enqueue path is the documented one-liner; verify it
    # writes a coalesced job when memory is enabled.
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    conn = main_module._db()
    mx.maybe_enqueue_extract_job(conn, "s1", "u1", provider_url="u",
        provider_key="k", provider_type="t", model_name="m", now=1000)
    assert conn.execute("SELECT COUNT(*) c FROM memory_extract_jobs").fetchone()["c"] == 1
