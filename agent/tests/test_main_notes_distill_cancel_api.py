import asyncio
import os
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import main
from notes import store as notes_store

H = {"X-User-Id": "u1"}
H2 = {"X-User-Id": "u2"}


def _client(tmp_path, monkeypatch):
    from db import init_db
    conn = init_db(str(tmp_path / "m.db"))
    notes_store.set_notes_root(conn, str(tmp_path / "Notes"))
    monkeypatch.setattr(main, "_db", lambda: conn)
    return TestClient(main.app), conn


def _seed(conn, path, status, user_id="u1", updated_at=100, origin="auto",
          attempts=0, last_error=None):
    conn.execute(
        "INSERT INTO notes_distill_jobs (file_path,user_id,root_id,file_mtime,"
        "status,attempts,origin,last_error,enqueued_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (path, user_id, "r1", 1, status, attempts, origin, last_error,
         50, updated_at))
    conn.commit()


def test_cancel_pending_job_tombstones_it(tmp_path, monkeypatch):
    c, conn = _client(tmp_path, monkeypatch)
    _seed(conn, "/DATA/a.pdf", "pending")
    r = c.post("/agent/notes/distill/jobs/cancel", headers=H,
               json={"path": "/DATA/a.pdf"})
    assert r.status_code == 200
    assert r.json() == {"cancelled": True}
    row = conn.execute(
        "SELECT status, last_error FROM notes_distill_jobs "
        "WHERE file_path=?", ("/DATA/a.pdf",)).fetchone()
    assert row["status"] == "skipped"
    assert row["last_error"] == "cancelled by user"


def test_cancel_moves_counts_from_pending_to_failed_bucket(tmp_path, monkeypatch):
    c, conn = _client(tmp_path, monkeypatch)
    _seed(conn, "/DATA/a.pdf", "pending")
    r = c.post("/agent/notes/distill/jobs/cancel", headers=H,
               json={"path": "/DATA/a.pdf"})
    assert r.status_code == 200
    body = c.get("/agent/notes/distill/jobs", headers=H).json()
    assert body["counts"]["pending"] == 0
    assert body["counts"]["failed"] == 1


def test_cancel_running_job_returns_409_and_leaves_row_untouched(tmp_path, monkeypatch):
    c, conn = _client(tmp_path, monkeypatch)
    _seed(conn, "/DATA/a.pdf", "running")
    r = c.post("/agent/notes/distill/jobs/cancel", headers=H,
               json={"path": "/DATA/a.pdf"})
    assert r.status_code == 409
    row = conn.execute(
        "SELECT status FROM notes_distill_jobs WHERE file_path=?",
        ("/DATA/a.pdf",)).fetchone()
    assert row["status"] == "running"


def test_cancel_nonexistent_path_returns_409(tmp_path, monkeypatch):
    c, conn = _client(tmp_path, monkeypatch)
    r = c.post("/agent/notes/distill/jobs/cancel", headers=H,
               json={"path": "/DATA/nope.pdf"})
    assert r.status_code == 409


def test_cancel_someone_elses_pending_job_returns_409_and_leaves_row_untouched(
        tmp_path, monkeypatch):
    c, conn = _client(tmp_path, monkeypatch)
    _seed(conn, "/DATA/b.pdf", "pending", user_id="u2")
    r = c.post("/agent/notes/distill/jobs/cancel", headers=H,
               json={"path": "/DATA/b.pdf"})
    assert r.status_code == 409
    row = conn.execute(
        "SELECT status, user_id FROM notes_distill_jobs WHERE file_path=?",
        ("/DATA/b.pdf",)).fetchone()
    assert row["status"] == "pending"
    assert row["user_id"] == "u2"


def test_cancelled_job_is_not_re_enqueued_by_the_scanner(tmp_path, monkeypatch):
    import notes_distill
    import notes_distill_scan

    c, conn = _client(tmp_path, monkeypatch)
    root = tmp_path / "root"
    root.mkdir()
    doc = root / "a.pdf"
    doc.write_text("x")
    mtime = int(os.stat(doc).st_mtime)

    notes_distill.enqueue(conn, file_path=str(doc), user_id="u1",
                          root_id="r1", file_mtime=mtime, origin="auto")

    r = c.post("/agent/notes/distill/jobs/cancel", headers=H,
               json={"path": str(doc)})
    assert r.status_code == 200

    known = notes_distill_scan._known_mtimes(conn, "u1")
    enqueued = asyncio.run(notes_distill_scan.scan_root(
        conn, user_id="u1", root_id="r1", root_path=str(root), known=known))
    assert enqueued == 0
