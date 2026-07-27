import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from db import init_db
import notes_distill
from notes import store as notes_store


def _conn(tmp_path):
    conn = init_db(str(tmp_path / "m.db"))
    notes_store.set_notes_root(conn, str(tmp_path / "Notes"))
    return conn


def test_extension_gate_accepts_documents_rejects_source_and_binary():
    assert notes_distill.is_distillable("/DATA/a/contract.pdf")
    assert notes_distill.is_distillable("/DATA/a/NOTES.MD")      # case-insensitive
    assert not notes_distill.is_distillable("/DATA/a/main.py")   # source code
    assert not notes_distill.is_distillable("/DATA/a/plan.dwg")  # binary
    assert not notes_distill.is_distillable("/DATA/a/README")    # no extension


def test_enqueue_coalesces_on_path(tmp_path):
    conn = _conn(tmp_path)
    assert notes_distill.enqueue(conn, file_path="/DATA/a.pdf", user_id="u1",
                                 root_id="r1", file_mtime=100, now=1000)
    assert notes_distill.enqueue(conn, file_path="/DATA/a.pdf", user_id="u1",
                                 root_id="r1", file_mtime=200, now=1100)
    rows = conn.execute("SELECT * FROM notes_distill_jobs").fetchall()
    assert len(rows) == 1
    assert rows[0]["file_mtime"] == 200
    assert rows[0]["enqueued_at"] == 1100
    assert rows[0]["attempts"] == 0


def test_enqueue_rejects_non_distillable_extension(tmp_path):
    conn = _conn(tmp_path)
    assert not notes_distill.enqueue(conn, file_path="/DATA/a.dwg",
                                     user_id="u1", root_id="r1",
                                     file_mtime=1, now=1)
    assert conn.execute("SELECT COUNT(*) c FROM notes_distill_jobs"
                        ).fetchone()["c"] == 0


def test_manual_origin_survives_auto_reenqueue(tmp_path):
    conn = _conn(tmp_path)
    notes_distill.enqueue(conn, file_path="/DATA/a.pdf", user_id="u1",
                          root_id="r1", file_mtime=1, origin="manual", now=1)
    notes_distill.enqueue(conn, file_path="/DATA/a.pdf", user_id="u1",
                          root_id="r1", file_mtime=2, origin="auto", now=2)
    row = conn.execute("SELECT origin FROM notes_distill_jobs").fetchone()
    assert row["origin"] == "manual"


def test_claim_prefers_manual_and_ignores_quota_for_it(tmp_path):
    conn = _conn(tmp_path)
    notes_distill.enqueue(conn, file_path="/DATA/old.pdf", user_id="u1",
                          root_id="r1", file_mtime=1, now=10)
    notes_distill.enqueue(conn, file_path="/DATA/new.pdf", user_id="u1",
                          root_id="r1", file_mtime=1, origin="manual", now=99)
    job = notes_distill.claim_job(conn, quota_ok=False, now=1000)
    assert job["file_path"] == "/DATA/new.pdf"
    assert job["status"] == "running"
    assert job["attempts"] == 1


def test_claim_returns_none_for_auto_when_quota_exhausted(tmp_path):
    conn = _conn(tmp_path)
    notes_distill.enqueue(conn, file_path="/DATA/a.pdf", user_id="u1",
                          root_id="r1", file_mtime=1, now=10)
    assert notes_distill.claim_job(conn, quota_ok=False, now=1000) is None
    assert notes_distill.claim_job(conn, quota_ok=True, now=1000) is not None


def test_fail_job_retries_then_tombstones_as_failed(tmp_path):
    conn = _conn(tmp_path)
    notes_distill.enqueue(conn, file_path="/DATA/a.pdf", user_id="u1",
                          root_id="r1", file_mtime=1, now=10)
    notes_distill.claim_job(conn, quota_ok=True, now=20)
    notes_distill.fail_job(conn, "/DATA/a.pdf", 1, ValueError("boom"), 30)
    row = conn.execute("SELECT * FROM notes_distill_jobs").fetchone()
    assert row["status"] == "pending" and "boom" in row["last_error"]

    notes_distill.claim_job(conn, quota_ok=True, now=40)
    notes_distill.fail_job(conn, "/DATA/a.pdf", 3, ValueError("boom"), 50)
    # At the attempts ceiling the row is tombstoned as 'failed', not deleted —
    # a DELETE would drop the file out of notes_distill_scan._known_mtimes and
    # cause an infinite re-enqueue/re-attempt loop on every scan pass.
    row = conn.execute("SELECT * FROM notes_distill_jobs").fetchone()
    assert row is not None
    assert row["status"] == "failed" and "boom" in row["last_error"]


def test_skip_job_tombstones_as_skipped_and_is_never_claimed(tmp_path):
    conn = _conn(tmp_path)
    notes_distill.enqueue(conn, file_path="/DATA/a.pdf", user_id="u1",
                          root_id="r1", file_mtime=1, now=10)
    notes_distill.claim_job(conn, quota_ok=True, now=20)
    notes_distill.skip_job(conn, "/DATA/a.pdf", "model unconfigured", 30)
    row = conn.execute("SELECT * FROM notes_distill_jobs").fetchone()
    assert row["status"] == "skipped"
    assert row["last_error"] == "model unconfigured"
    assert notes_distill.claim_job(conn, quota_ok=True, now=40) is None


def test_requeue_orphaned_flips_running_back(tmp_path):
    conn = _conn(tmp_path)
    notes_distill.enqueue(conn, file_path="/DATA/a.pdf", user_id="u1",
                          root_id="r1", file_mtime=1, now=10)
    notes_distill.claim_job(conn, quota_ok=True, now=20)
    assert notes_distill.requeue_orphaned(conn) == 1
    assert conn.execute("SELECT status FROM notes_distill_jobs"
                        ).fetchone()["status"] == "pending"
