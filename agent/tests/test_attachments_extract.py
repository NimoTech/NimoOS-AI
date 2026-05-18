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
