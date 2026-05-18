import importlib
import json
import os
import time
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "agent.db"))
    monkeypatch.setenv("NIMOOS_AGENT_DATA_ROOT", str(tmp_path))
    import db as db_module
    importlib.reload(db_module)
    import main as main_module
    importlib.reload(main_module)
    main_module._db().execute(
        "INSERT INTO sessions (id,user_id,created_at,updated_at) VALUES (?,?,0,0)",
        ("sess1", "u1"))
    main_module._db().commit()
    return TestClient(main_module.app), main_module, tmp_path


def _hdr():
    return {"X-User-Id": "u1"}


def _fake_extract(markdown="hello", pages=1, extractor="fake",
                  truncated=False, error=None):
    """Return a function suitable for monkeypatching extract.extract_to_markdown."""
    def fn(path, ext, *, max_chars, max_uncompressed_bytes):
        if error is not None:
            return {"ok": False, "error": error}
        return {"ok": True, "markdown": markdown, "pages": pages,
                "chars": len(markdown), "truncated": truncated,
                "extractor": extractor}
    return fn


def test_document_success_writes_sidecar(client, monkeypatch):
    c, m, root = client
    from attachments import extract
    monkeypatch.setattr(extract, "extract_to_markdown",
                        _fake_extract(markdown="# Title\n\nbody", pages=3,
                                      extractor="pypdf"))
    r = c.post("/agent/sessions/sess1/attachments",
               files={"file": ("report.pdf", b"%PDF-1.4\n", "application/pdf")},
               headers=_hdr())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "document"

    row = m._db().execute(
        "SELECT rel_path, meta_json FROM attachments WHERE id=?",
        (body["id"],)).fetchone()
    rel_path, meta_json = row[0], row[1]
    meta = json.loads(meta_json)
    assert meta["extractor"] == "pypdf"
    assert meta["pages"] == 3
    sidecar = meta["sidecar"]
    assert sidecar == rel_path + ".md"
    sidecar_path = root / "sessions" / "sess1" / "attachments" / sidecar
    assert sidecar_path.exists()
    assert sidecar_path.read_text(encoding="utf-8") == "# Title\n\nbody"


def test_document_extract_failure_keeps_document_kind(client, monkeypatch):
    c, m, root = client
    from attachments import extract
    monkeypatch.setattr(extract, "extract_to_markdown",
                        _fake_extract(error="empty_scanned"))
    r = c.post("/agent/sessions/sess1/attachments",
               files={"file": ("scan.pdf", b"%PDF-1.4\n", "application/pdf")},
               headers=_hdr())
    assert r.status_code == 201
    body = r.json()
    assert body["kind"] == "document"
    meta = json.loads(m._db().execute(
        "SELECT meta_json FROM attachments WHERE id=?",
        (body["id"],)).fetchone()[0])
    assert meta == {"extract_error": "empty_scanned"}
    # No sidecar on disk.
    assert not list((root / "sessions" / "sess1" / "attachments").glob("*.md"))


def test_document_not_installed_downgrades_to_binary(client, monkeypatch):
    c, m, root = client
    from attachments import extract
    monkeypatch.setattr(extract, "extract_to_markdown",
                        _fake_extract(error="not_installed"))
    r = c.post("/agent/sessions/sess1/attachments",
               files={"file": ("x.docx", b"PK\x03\x04", "application/octet-stream")},
               headers=_hdr())
    assert r.status_code == 201
    body = r.json()
    assert body["kind"] == "binary"
    meta = json.loads(m._db().execute(
        "SELECT meta_json FROM attachments WHERE id=?",
        (body["id"],)).fetchone()[0])
    assert meta == {"extract_error": "not_installed"}


def test_document_sidecar_write_failure_degrades_gracefully(client, monkeypatch):
    """Upload still 200; row is kind=document with extract_error sidecar_write_failed;
    the original file is on disk; no sidecar."""
    c, m, root = client
    from attachments import extract, upload as upload_module
    monkeypatch.setattr(extract, "extract_to_markdown",
                        _fake_extract(markdown="content"))

    # Patch upload_module._write_sidecar to raise.
    def boom(*_a, **_kw):
        raise OSError("disk full")
    monkeypatch.setattr(upload_module, "_write_sidecar", boom)

    r = c.post("/agent/sessions/sess1/attachments",
               files={"file": ("r.pdf", b"%PDF-1.4\n", "application/pdf")},
               headers=_hdr())
    assert r.status_code == 201
    body = r.json()
    assert body["kind"] == "document"
    meta = json.loads(m._db().execute(
        "SELECT meta_json, rel_path FROM attachments WHERE id=?",
        (body["id"],)).fetchone()[0])
    assert meta == {"extract_error": "sidecar_write_failed"}
    # Original file still present.
    rel = m._db().execute(
        "SELECT rel_path FROM attachments WHERE id=?",
        (body["id"],)).fetchone()[0]
    assert (root / "sessions" / "sess1" / "attachments" / rel).exists()
    # No sidecar.
    assert not list((root / "sessions" / "sess1" / "attachments").glob("*.md"))


def test_document_extraction_timeout(client, monkeypatch):
    """A slow extractor exceeding MAX_DOC_EXTRACT_SECONDS is recorded as
    extract_error=timeout; upload still succeeds within ~timeout + slack."""
    c, m, _ = client
    from attachments import extract
    m.MAX_DOC_EXTRACT_SECONDS = 1  # tighten for fast test

    def slow(path, ext, *, max_chars, max_uncompressed_bytes):
        time.sleep(2.5)
        return {"ok": True, "markdown": "late", "pages": 1, "chars": 4,
                "truncated": False, "extractor": "slow"}
    monkeypatch.setattr(extract, "extract_to_markdown", slow)

    t0 = time.time()
    r = c.post("/agent/sessions/sess1/attachments",
               files={"file": ("slow.pdf", b"%PDF-1.4\n", "application/pdf")},
               headers=_hdr())
    elapsed = time.time() - t0
    assert r.status_code == 201
    assert r.json()["kind"] == "document"
    meta = json.loads(m._db().execute(
        "SELECT meta_json FROM attachments WHERE id=?",
        (r.json()["id"],)).fetchone()[0])
    assert meta == {"extract_error": "timeout"}
    # Should NOT have waited the full 2.5 s.
    assert elapsed < 2.0, f"upload took {elapsed:.2f}s, expected ~1s"
