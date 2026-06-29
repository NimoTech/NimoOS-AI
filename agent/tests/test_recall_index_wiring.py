import main as main_module
import recall_index as ri


def test_main_registers_recall_worker_startup():
    names = [h.__name__ for h in main_module.app.router.on_startup]
    assert "_recall_worker_startup" in names


def test_enqueue_helper_writes_job(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    conn = main_module._db()
    assert ri.maybe_enqueue_index_job(conn, "s1", "u1", now=1000) is True
    assert conn.execute("SELECT COUNT(*) c FROM recall_index_jobs"
                        ).fetchone()["c"] == 1
