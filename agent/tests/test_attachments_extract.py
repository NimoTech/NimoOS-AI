import pytest


def test_unsupported_extension_returns_error(tmp_path):
    from attachments.extract import extract_to_markdown
    p = tmp_path / "x.xyz"
    p.write_bytes(b"hello")
    result = extract_to_markdown(str(p), "xyz",
                                 max_chars=1000,
                                 max_uncompressed_bytes=10_000_000)
    assert result == {"ok": False, "error": "unsupported"}


def test_missing_library_returns_not_installed(tmp_path, monkeypatch):
    """If the per-format library can't be imported, the dispatcher reports
    not_installed for that extension only."""
    from attachments import extract

    # Force the pdf branch's import to fail by stubbing builtins.__import__.
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pypdf":
            raise ImportError("test forcibly hides pypdf")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.4\n")
    result = extract.extract_to_markdown(str(p), "pdf",
                                         max_chars=1000,
                                         max_uncompressed_bytes=10_000_000)
    assert result == {"ok": False, "error": "not_installed"}


import os


def _fixture(name: str) -> str:
    return os.path.join(os.path.dirname(__file__), "fixtures", name)


def test_pdf_happy_path():
    from attachments.extract import extract_to_markdown
    result = extract_to_markdown(_fixture("tiny.pdf"), "pdf",
                                 max_chars=10_000,
                                 max_uncompressed_bytes=10_000_000)
    assert result["ok"] is True
    assert "Hello PDF" in result["markdown"]
    assert result["pages"] == 1
    assert result["extractor"] == "pypdf"
    assert result["truncated"] is False


def test_pdf_truncation_sets_flag(monkeypatch):
    """With max_chars below the extracted length, extractor stops early."""
    from attachments.extract import extract_to_markdown
    # max_chars=5 forces truncation since "Hello PDF" is 9 chars.
    result = extract_to_markdown(_fixture("tiny.pdf"), "pdf",
                                 max_chars=5,
                                 max_uncompressed_bytes=10_000_000)
    assert result["ok"] is True
    assert result["truncated"] is True
    assert len(result["markdown"]) <= 5


def test_pdf_garbage_returns_parse_error(tmp_path):
    from attachments.extract import extract_to_markdown
    p = tmp_path / "junk.pdf"
    p.write_bytes(b"this is not a pdf at all")
    result = extract_to_markdown(str(p), "pdf",
                                 max_chars=1000,
                                 max_uncompressed_bytes=10_000_000)
    assert result["ok"] is False
    assert result["error"] == "parse_error"


def test_pdf_encrypted_returns_encrypted(tmp_path, monkeypatch):
    """We don't need an actual encrypted PDF — monkeypatch pypdf to claim
    is_encrypted=True and refuse decrypt('')."""
    from attachments import extract
    import pypdf

    class FakeReader:
        is_encrypted = True
        pages: list = []
        def __init__(self, *_a, **_kw): pass
        def decrypt(self, _pw): return 0  # 0 = failure in pypdf API

    monkeypatch.setattr(pypdf, "PdfReader", FakeReader)
    p = tmp_path / "enc.pdf"
    p.write_bytes(b"%PDF-1.4\n")  # presence only; FakeReader ignores content
    result = extract.extract_to_markdown(str(p), "pdf",
                                         max_chars=1000,
                                         max_uncompressed_bytes=10_000_000)
    assert result == {"ok": False, "error": "encrypted"}


def test_pdf_empty_extraction_returns_empty_scanned(tmp_path, monkeypatch):
    """Simulate a scanned PDF: pypdf yields whitespace-only text."""
    from attachments import extract
    import pypdf

    class FakePage:
        def extract_text(self): return "   \n  "

    class FakeReader:
        is_encrypted = False
        pages = [FakePage()]
        def __init__(self, *_a, **_kw): pass

    monkeypatch.setattr(pypdf, "PdfReader", FakeReader)
    p = tmp_path / "scan.pdf"
    p.write_bytes(b"%PDF-1.4\n")
    result = extract.extract_to_markdown(str(p), "pdf",
                                         max_chars=1000,
                                         max_uncompressed_bytes=10_000_000)
    assert result == {"ok": False, "error": "empty_scanned"}


def test_pdf_unexpected_exception_caught(tmp_path, monkeypatch):
    """Even an exception type we never anticipated must not escape."""
    from attachments import extract
    import pypdf

    class ExplodingReader:
        def __init__(self, *_a, **_kw):
            raise RecursionError("simulated pypdf pathology")

    monkeypatch.setattr(pypdf, "PdfReader", ExplodingReader)
    p = tmp_path / "boom.pdf"
    p.write_bytes(b"%PDF-1.4\n")
    result = extract.extract_to_markdown(str(p), "pdf",
                                         max_chars=1000,
                                         max_uncompressed_bytes=10_000_000)
    assert result == {"ok": False, "error": "parse_error"}
