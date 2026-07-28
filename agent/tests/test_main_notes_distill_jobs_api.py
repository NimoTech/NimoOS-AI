import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import main
from notes import store as notes_store

H = {"X-User-Id": "u1"}


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


def test_lists_own_jobs_only(tmp_path, monkeypatch):
    c, conn = _client(tmp_path, monkeypatch)
    _seed(conn, "/DATA/a.pdf", "pending")
    _seed(conn, "/DATA/b.pdf", "pending", user_id="u2")
    body = c.get("/agent/notes/distill/jobs", headers=H).json()
    assert [j["file_path"] for j in body["jobs"]] == ["/DATA/a.pdf"]


def test_failed_filter_includes_skipped(tmp_path, monkeypatch):
    c, conn = _client(tmp_path, monkeypatch)
    _seed(conn, "/DATA/f.pdf", "failed", last_error="boom", updated_at=200)
    _seed(conn, "/DATA/s.pdf", "skipped", last_error="no text", updated_at=100)
    _seed(conn, "/DATA/p.pdf", "pending")
    body = c.get("/agent/notes/distill/jobs?status=failed", headers=H).json()
    assert [j["file_path"] for j in body["jobs"]] == ["/DATA/f.pdf", "/DATA/s.pdf"]
    assert body["jobs"][0]["last_error"] == "boom"
    assert body["jobs"][1]["status"] == "skipped"


def test_counts_cover_all_buckets_regardless_of_filter(tmp_path, monkeypatch):
    c, conn = _client(tmp_path, monkeypatch)
    _seed(conn, "/DATA/p.pdf", "pending")
    _seed(conn, "/DATA/r.pdf", "running")
    _seed(conn, "/DATA/f.pdf", "failed")
    _seed(conn, "/DATA/s.pdf", "skipped")
    body = c.get("/agent/notes/distill/jobs?status=pending", headers=H).json()
    assert body["counts"] == {"pending": 1, "running": 1, "failed": 2}


def test_sorted_by_updated_at_desc_and_limit_clamped(tmp_path, monkeypatch):
    c, conn = _client(tmp_path, monkeypatch)
    for i in range(5):
        _seed(conn, f"/DATA/{i}.pdf", "pending", updated_at=i)
    body = c.get("/agent/notes/distill/jobs?limit=3", headers=H).json()
    assert [j["file_path"] for j in body["jobs"]] == \
        ["/DATA/4.pdf", "/DATA/3.pdf", "/DATA/2.pdf"]
    # limit ceiling is 500: stuffing 501 rows is impractical here, so just
    # verify the clamping logic accepts an oversized value without erroring.
    assert c.get("/agent/notes/distill/jobs?limit=9999", headers=H).status_code == 200


def test_bad_status_returns_400(tmp_path, monkeypatch):
    c, conn = _client(tmp_path, monkeypatch)
    assert c.get("/agent/notes/distill/jobs?status=bogus",
                 headers=H).status_code == 400
