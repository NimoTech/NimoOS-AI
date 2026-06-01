import os
import importlib
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Override DB and data paths for isolation
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "agent.db"))
    monkeypatch.setenv("NIMOOS_AGENT_DATA_ROOT", str(tmp_path))
    import db as db_module
    importlib.reload(db_module)
    import main as main_module
    importlib.reload(main_module)
    # Create a session owned by u1
    main_module._db().execute(
        "INSERT INTO sessions (id,user_id,created_at,updated_at) VALUES (?,?,0,0)",
        ("sess1", "u1"))
    main_module._db().commit()
    return TestClient(main_module.app), main_module


def _hdr():
    return {"X-User-Id": "u1"}


def test_upload_text_returns_kind_text(client):
    c, _ = client
    r = c.post("/agent/sessions/sess1/attachments",
               files={"file": ("notes.txt", b"hello", "text/plain")},
               headers=_hdr())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "text"
    assert body["filename"] == "notes.txt"
    assert body["size_bytes"] == 5
    assert body["id"].startswith("att_") or len(body["id"]) > 0


def test_upload_png_kind_image(client):
    c, _ = client
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    r = c.post("/agent/sessions/sess1/attachments",
               files={"file": ("photo.png", png, "image/png")},
               headers=_hdr())
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "image"


def test_upload_size_over_global_limit_returns_413(client):
    c, m = client
    m.MAX_ATTACHMENT_SIZE = 10
    r = c.post("/agent/sessions/sess1/attachments",
               files={"file": ("big.bin", b"a" * 100, "application/octet-stream")},
               headers=_hdr())
    assert r.status_code == 413


def test_upload_image_over_image_limit_returns_413(client):
    c, m = client
    m.MAX_IMAGE_ATTACHMENT_SIZE = 8
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    r = c.post("/agent/sessions/sess1/attachments",
               files={"file": ("big.png", png, "image/png")},
               headers=_hdr())
    assert r.status_code == 413
    assert "image" in r.json().get("detail", "").lower()


def test_session_count_limit(client):
    c, m = client
    m.MAX_ATTACHMENTS_PER_SESSION = 1
    r1 = c.post("/agent/sessions/sess1/attachments",
                files={"file": ("a.txt", b"x", "text/plain")},
                headers=_hdr())
    assert r1.status_code == 201
    r2 = c.post("/agent/sessions/sess1/attachments",
                files={"file": ("b.txt", b"x", "text/plain")},
                headers=_hdr())
    assert r2.status_code == 409


def test_session_ownership_required(client):
    c, _ = client
    r = c.post("/agent/sessions/sess1/attachments",
               files={"file": ("a.txt", b"x", "text/plain")},
               headers={"X-User-Id": "someone-else"})
    assert r.status_code in (403, 404)  # _assert_owns_session may use either


def test_long_filename_preserves_extension(client):
    c, m = client
    name = ("a" * 300) + ".mp4"
    r = c.post("/agent/sessions/sess1/attachments",
               files={"file": (name, b"\x00" * 16, "video/mp4")},
               headers=_hdr())
    assert r.status_code == 201
    body = r.json()
    assert body["filename"].endswith(".mp4")
    # And the underlying rel_path also keeps .mp4
    rel = m._db().execute(
        "SELECT rel_path FROM attachments WHERE id=?", (body["id"],)
    ).fetchone()[0]
    assert rel.endswith(".mp4")
