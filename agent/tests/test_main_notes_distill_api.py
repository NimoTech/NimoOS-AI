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
