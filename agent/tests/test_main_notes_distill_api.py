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


def test_settings_roundtrip_includes_distill_fields(tmp_path, monkeypatch):
    c, conn = _client(tmp_path, monkeypatch)
    r = c.get("/agent/notes/settings", headers=H)
    assert r.status_code == 200
    assert r.json()["distill_roots"] == []
    assert r.json()["distill_daily_cap"] == 50

    r = c.put("/agent/notes/settings", headers=H, json={
        "distill_roots": ["r1"], "distill_daily_cap": 10,
        "background_model": "cloud:2:m"})
    assert r.status_code == 200
    body = c.get("/agent/notes/settings", headers=H).json()
    assert body["distill_roots"] == ["r1"]
    assert body["distill_daily_cap"] == 10
    assert body["background_model"] == "cloud:2:m"


def test_manual_distill_enqueues(tmp_path, monkeypatch):
    c, conn = _client(tmp_path, monkeypatch)
    doc = tmp_path / "a.pdf"
    doc.write_text("x")
    monkeypatch.setattr(main, "_distill_gate_ok", lambda uid, p: True)
    r = c.post("/agent/notes/distill", headers=H, json={"path": str(doc)})
    assert r.status_code == 200 and r.json()["queued"] is True
    row = conn.execute("SELECT origin FROM notes_distill_jobs").fetchone()
    assert row["origin"] == "manual"


def test_manual_distill_rejects_bad_extension(tmp_path, monkeypatch):
    c, conn = _client(tmp_path, monkeypatch)
    doc = tmp_path / "a.dwg"
    doc.write_text("x")
    monkeypatch.setattr(main, "_distill_gate_ok", lambda uid, p: True)
    r = c.post("/agent/notes/distill", headers=H, json={"path": str(doc)})
    assert r.status_code == 400


def test_manual_distill_respects_fs_gate(tmp_path, monkeypatch):
    c, conn = _client(tmp_path, monkeypatch)
    doc = tmp_path / "a.pdf"
    doc.write_text("x")
    monkeypatch.setattr(main, "_distill_gate_ok", lambda uid, p: False)
    r = c.post("/agent/notes/distill", headers=H, json={"path": str(doc)})
    assert r.status_code == 403


def test_manual_distill_404_for_missing_file(tmp_path, monkeypatch):
    c, conn = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "_distill_gate_ok", lambda uid, p: True)
    r = c.post("/agent/notes/distill", headers=H,
               json={"path": str(tmp_path / "nope.pdf")})
    assert r.status_code == 404


def test_status_reports_counts(tmp_path, monkeypatch):
    c, conn = _client(tmp_path, monkeypatch)
    import notes_distill
    notes_distill.enqueue(conn, file_path="/DATA/a.pdf", user_id="u1",
                          root_id="r1", file_mtime=1)
    body = c.get("/agent/notes/distill/status", headers=H).json()
    assert body["pending"] == 1
    assert body["distilled"] == 0
    assert body["quota_remaining"] == 50


def test_status_pending_count_excludes_tombstoned_jobs(tmp_path, monkeypatch):
    """C3: 'failed'/'skipped' tombstones must not inflate the pending count
    the settings panel shows the user."""
    c, conn = _client(tmp_path, monkeypatch)
    import notes_distill
    notes_distill.enqueue(conn, file_path="/DATA/a.pdf", user_id="u1",
                          root_id="r1", file_mtime=1, now=1)
    notes_distill.enqueue(conn, file_path="/DATA/b.pdf", user_id="u1",
                          root_id="r1", file_mtime=1, now=2)
    notes_distill.enqueue(conn, file_path="/DATA/c.pdf", user_id="u1",
                          root_id="r1", file_mtime=1, now=3)
    notes_distill.claim_job(conn, quota_ok=True, now=10)   # claims a.pdf
    notes_distill.fail_job(conn, "/DATA/a.pdf", notes_distill.MAX_ATTEMPTS,
                           ValueError("boom"), 11)
    notes_distill.claim_job(conn, quota_ok=True, now=12)   # claims b.pdf
    notes_distill.skip_job(conn, "/DATA/b.pdf", "model unconfigured", 13)
    statuses = {r["file_path"]: r["status"] for r in
                conn.execute("SELECT file_path, status FROM notes_distill_jobs")}
    assert statuses == {"/DATA/a.pdf": "failed", "/DATA/b.pdf": "skipped",
                        "/DATA/c.pdf": "pending"}

    body = c.get("/agent/notes/distill/status", headers=H).json()
    assert body["pending"] == 1


def test_manual_distill_denies_path_outside_data_without_any_gate_stub(
        tmp_path, monkeypatch):
    """(e): unmocked fs_gate integration — /etc/passwd is never under /DATA,
    so the headless deny-only gate (mcp_server/fs_gate.py) must 403 it on its
    own, with no monkeypatch of _distill_gate_ok."""
    c, conn = _client(tmp_path, monkeypatch)
    r = c.post("/agent/notes/distill", headers=H, json={"path": "/etc/passwd"})
    assert r.status_code == 403
