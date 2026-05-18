# Document Attachment Extraction (Phase 1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new `kind="document"` attachment that extracts PDF/DOCX/XLSX/PPTX uploads to Markdown sidecars (pure-Python libs, threadpool + timeout, zip-bomb precheck), and surface success-with-content **or** structured failure to the agent via the existing `read_attachment` tool.

**Architecture:** New `attachments/extract.py` module dispatches by extension and writes Markdown alongside the upload. `handle_upload` becomes async and runs extraction via `asyncio.wait_for(asyncio.to_thread(...))` so CPU-bound parsing doesn't block the event loop. Extraction failures keep `kind="document"` with `meta.extract_error` set — only `not_installed` downgrades to `binary`. Spec: `docs/superpowers/specs/2026-05-18-document-attachment-extraction-design.md`.

**Tech Stack:** Python 3.11+, FastAPI, pytest, pypdf, python-docx, openpyxl (read_only/data_only), python-pptx, stdlib `zipfile`/`asyncio`.

**Cwd for all commands:** `/home/nimo/nimoos/NimoOS-AI`. All git operations are inside this sub-repo (the parent `/home/nimo/nimoos` is not a git repo).

---

## File Structure

**New files:**
- `agent/attachments/extract.py` — public `extract_to_markdown(path, ext, *, max_chars, max_uncompressed_bytes)` plus per-format private helpers.
- `agent/tests/test_attachments_extract.py` — unit tests per format incl. zip-bomb / truncation / encrypted / pypdf-exception-envelope / openpyxl-empty-cache fallback.
- `agent/tests/test_attachments_upload_document.py` — endpoint-level tests for the document branch in `handle_upload` (success, extractor failure, sidecar-write failure, timeout, DB error).
- `agent/tests/fixtures/tiny.pdf` — committed minimal PDF used by the pypdf happy-path test (everything else is built in-memory in the test).

**Modified files:**
- `agent/attachments/kind.py` — add `DOCUMENT_EXT_MAP`, route the five extensions to `kind="document"`.
- `agent/attachments/upload.py` — `handle_upload` becomes `async def`, gains 3 kwargs (`max_doc_chars`, `max_doc_uncompressed_bytes`, `max_extract_seconds`), inserts a `document` branch and sidecar writing.
- `agent/main.py` — three new env-driven config constants; `await` the now-async `handle_upload`; pass the new kwargs.
- `agent/skills/attachments.py` — new `document` branch (sidecar success path + `extract_error` failure path); updated tool docstring.
- `agent/agent.py` — small copy tweak in `attachment_system_block` so the model knows document errors may surface.
- `agent/tests/test_attachments_kind.py` — five new cases for the five document extensions.
- `agent/tests/test_read_attachment_tool.py` — three new cases for `kind=document`.
- `agent/requirements.txt` — add four lines.

---

## Task 1: Classify documents by extension in `kind.py`

**Files:**
- Modify: `agent/attachments/kind.py`
- Test: `agent/tests/test_attachments_kind.py`

The new `kind="document"` is decided by extension (no parsing). It must run **after** image/video/audio (so a PDF that magic detects as `application/pdf` isn't shadowed) and **before** the text whitelist (which doesn't include these extensions today anyway, but order it explicitly).

- [ ] **Step 1: Write failing tests for the five document extensions**

Edit `agent/tests/test_attachments_kind.py`, append:

```python
import pytest

DOC_EXTS_AND_MIMES = [
    ("a.pdf",  "application/pdf"),
    ("a.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("a.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ("a.xlsm", "application/vnd.ms-excel.sheet.macroEnabled.12"),
    ("a.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
]


@pytest.mark.parametrize("name,expected_mime", DOC_EXTS_AND_MIMES)
def test_document_extension_classified_as_document(tmp_path, name, expected_mime):
    # Bytes don't have to be a real document; classify is by extension.
    path = tmp_path / name
    path.write_bytes(b"not really a document, just placeholder bytes")
    from attachments.kind import classify
    mime, kind = classify(str(path), name)
    assert kind == "document", f"{name} should be document, got {kind}"
    assert mime == expected_mime
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/nimo/nimoos/NimoOS-AI/agent && python -m pytest tests/test_attachments_kind.py -v`
Expected: FAIL for the five new parametrized cases (kind comes back as `binary`).

- [ ] **Step 3: Add `DOCUMENT_EXT_MAP` and the new classify branch**

Edit `agent/attachments/kind.py`. After `TEXT_EXT_WHITELIST = {...}` add:

```python
DOCUMENT_EXT_MAP = {
    "pdf":  "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
```

Inside `classify()`, after the image/video/audio block and **before** the `ext = os.path.splitext(...)` line, insert:

```python
    ext = os.path.splitext(original_name)[1].lstrip(".").lower()
    if ext in DOCUMENT_EXT_MAP:
        return DOCUMENT_EXT_MAP[ext], "document"
```

Then remove the now-duplicate `ext = ...` line below (the existing text-whitelist branch). The final function reads:

```python
def classify(path: str, original_name: str) -> tuple[str, str]:
    mime = _detect_mime(path, original_name)
    major = mime.split("/", 1)[0] if "/" in mime else ""

    if major == "image":
        return mime, "image"
    if major == "video":
        return mime, "video"
    if major == "audio":
        return mime, "audio"

    ext = os.path.splitext(original_name)[1].lstrip(".").lower()
    if ext in DOCUMENT_EXT_MAP:
        return DOCUMENT_EXT_MAP[ext], "document"

    if ext in TEXT_EXT_WHITELIST and _is_utf8_text(path):
        return mime if major == "text" else "text/plain", "text"

    if major == "text" and _is_utf8_text(path):
        return mime, "text"

    return mime, "binary"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/nimo/nimoos/NimoOS-AI/agent && python -m pytest tests/test_attachments_kind.py -v`
Expected: PASS for all (including the 5 new ones and the existing tests).

- [ ] **Step 5: Commit**

```bash
cd /home/nimo/nimoos/NimoOS-AI
git add agent/attachments/kind.py agent/tests/test_attachments_kind.py
git commit -m "$(cat <<'EOF'
feat(agent): classify pdf/docx/xlsx/pptx as kind=document

Speculative extension-based classification — actual parsing happens in
handle_upload. Other formats unchanged.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Add `attachments/extract.py` skeleton (dispatcher + ImportError + unsupported)

**Files:**
- Create: `agent/attachments/extract.py`
- Test: `agent/tests/test_attachments_extract.py`

Establish the public API and the error envelope before plugging in per-format implementations. The per-format helpers will be added in later tasks; this task only wires the dispatcher and the `unsupported` / `not_installed` outcomes.

- [ ] **Step 1: Write failing tests for the dispatcher**

Create `agent/tests/test_attachments_extract.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/nimo/nimoos/NimoOS-AI/agent && python -m pytest tests/test_attachments_extract.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'attachments.extract'`).

- [ ] **Step 3: Create the dispatcher with all branches stubbed**

Create `agent/attachments/extract.py`:

```python
"""Document-to-Markdown extractors. Called from attachments.upload.handle_upload
on a worker thread (asyncio.to_thread); functions here are blocking and CPU
bound and MUST NOT be awaited directly.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def extract_to_markdown(path: str, ext: str, *,
                        max_chars: int,
                        max_uncompressed_bytes: int) -> dict:
    """Dispatch a document path to the matching format extractor.

    Returns:
      success: {"ok": True, "markdown": str, "pages": int|None,
                "chars": int, "truncated": bool, "extractor": str}
      failure: {"ok": False, "error": str}
        error in {not_installed, unsupported, zip_bomb,
                  parse_error, encrypted, empty_scanned}
    """
    ext = ext.lower()
    if ext == "pdf":
        return _extract_pdf(path, max_chars=max_chars)
    if ext == "docx":
        return _extract_docx(path, max_chars=max_chars,
                             max_uncompressed_bytes=max_uncompressed_bytes)
    if ext in ("xlsx", "xlsm"):
        return _extract_xlsx(path, max_chars=max_chars,
                             max_uncompressed_bytes=max_uncompressed_bytes)
    if ext == "pptx":
        return _extract_pptx(path, max_chars=max_chars,
                             max_uncompressed_bytes=max_uncompressed_bytes)
    return {"ok": False, "error": "unsupported"}


# Per-format implementations are added in later tasks. Stubs return
# not_installed so the dispatcher is fully exercisable today.
def _extract_pdf(path: str, *, max_chars: int) -> dict:
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return {"ok": False, "error": "not_installed"}
    return {"ok": False, "error": "not_installed"}  # replaced in Task 3


def _extract_docx(path: str, *, max_chars: int,
                  max_uncompressed_bytes: int) -> dict:
    try:
        import docx  # noqa: F401  (python-docx exports as `docx`)
    except ImportError:
        return {"ok": False, "error": "not_installed"}
    return {"ok": False, "error": "not_installed"}  # replaced in Task 4


def _extract_xlsx(path: str, *, max_chars: int,
                  max_uncompressed_bytes: int) -> dict:
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        return {"ok": False, "error": "not_installed"}
    return {"ok": False, "error": "not_installed"}  # replaced in Task 5


def _extract_pptx(path: str, *, max_chars: int,
                  max_uncompressed_bytes: int) -> dict:
    try:
        import pptx  # noqa: F401
    except ImportError:
        return {"ok": False, "error": "not_installed"}
    return {"ok": False, "error": "not_installed"}  # replaced in Task 6
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/nimo/nimoos/NimoOS-AI/agent && python -m pytest tests/test_attachments_extract.py -v`
Expected: PASS for both tests.

- [ ] **Step 5: Commit**

```bash
cd /home/nimo/nimoos/NimoOS-AI
git add agent/attachments/extract.py agent/tests/test_attachments_extract.py
git commit -m "$(cat <<'EOF'
feat(agent): scaffold attachments.extract dispatcher

Public extract_to_markdown(path, ext, ...) returns structured ok/error
dict. Per-format branches currently stub to not_installed; subsequent
commits fill in pdf/docx/xlsx/pptx implementations.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: PDF extractor (`pypdf`)

**Files:**
- Modify: `agent/attachments/extract.py` (the `_extract_pdf` function)
- Modify: `agent/tests/test_attachments_extract.py`
- Create: `agent/tests/fixtures/tiny.pdf`

PDF gets:
1. Encryption detection up front (`reader.is_encrypted` → `error="encrypted"` if `decrypt("")` fails).
2. Page-by-page text accumulation with `max_chars` truncation.
3. Empty-after-strip → `error="empty_scanned"`.
4. Broad `except Exception` wrapping the entire body → `error="parse_error"`. pypdf is known to raise unexpected exception types (and even `RecursionError`) on adversarial inputs.

- [ ] **Step 1: Generate the tiny PDF fixture**

We need one real PDF to anchor the happy-path test. We use pypdf to write a minimal 1-page PDF. (pypdf supports creating PDFs by adding blank pages, then we can run the test against it — extract_text on a blank page returns "", which we *don't* want; so we use a slightly bigger fixture below.)

Easier: hand-write a minimal PDF. Run this one-off command and commit the output:

```bash
cd /home/nimo/nimoos/NimoOS-AI/agent
mkdir -p tests/fixtures
python - <<'PY'
# Build a 1-page PDF with a visible "Hello PDF" text token. This is the
# smallest hand-rollable PDF that pypdf can extract text from.
import io, zlib
def obj(n, body):
    return f"{n} 0 obj\n{body}\nendobj\n".encode("latin-1")

content_stream = b"BT /F1 24 Tf 100 700 Td (Hello PDF) Tj ET"
content = b"<< /Length " + str(len(content_stream)).encode() + b" >>\nstream\n" + content_stream + b"\nendstream"

objs = [
    obj(1, "<< /Type /Catalog /Pages 2 0 R >>"),
    obj(2, "<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
    obj(3, "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"),
    b"4 0 obj\n" + content + b"\nendobj\n",
    obj(5, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"),
]

buf = b"%PDF-1.4\n"
offsets = []
for o in objs:
    offsets.append(len(buf))
    buf += o
xref_off = len(buf)
buf += b"xref\n0 6\n0000000000 65535 f \n"
for off in offsets:
    buf += f"{off:010d} 00000 n \n".encode()
buf += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref_off).encode() + b"\n%%EOF\n"

open("tests/fixtures/tiny.pdf", "wb").write(buf)
print("wrote", len(buf), "bytes")
PY
ls -la tests/fixtures/tiny.pdf
```

Verify the fixture is a working PDF:

```bash
cd /home/nimo/nimoos/NimoOS-AI/agent
python -c "import pypdf; r = pypdf.PdfReader('tests/fixtures/tiny.pdf'); print(repr(r.pages[0].extract_text()))"
```
Expected: prints something containing `"Hello PDF"` (whitespace/positioning may vary). If pypdf can't extract any text, adjust the fixture (e.g., add `/F1 1 Tf` tokens) until it can — without working text extraction the test below is meaningless.

- [ ] **Step 2: Write failing tests for the PDF extractor**

Append to `agent/tests/test_attachments_extract.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /home/nimo/nimoos/NimoOS-AI/agent && python -m pytest tests/test_attachments_extract.py -v`
Expected: the 6 new PDF tests FAIL (current stub returns `not_installed`).

- [ ] **Step 4: Implement `_extract_pdf`**

Replace the stub body in `agent/attachments/extract.py`:

```python
def _extract_pdf(path: str, *, max_chars: int) -> dict:
    try:
        import pypdf
    except ImportError:
        return {"ok": False, "error": "not_installed"}

    try:
        reader = pypdf.PdfReader(path)

        if reader.is_encrypted:
            # decrypt('') returns 0 when the empty password fails, 1/2 on success.
            try:
                ok = reader.decrypt("")
            except Exception:
                ok = 0
            if not ok:
                return {"ok": False, "error": "encrypted"}

        pages = list(reader.pages)
        buf: list[str] = []
        total = 0
        truncated = False
        for page in pages:
            try:
                text = page.extract_text() or ""
            except Exception:
                # Per-page failure shouldn't sink the whole document; emit a
                # marker so the model knows that page was lost.
                text = "[unreadable page]"
            if total + len(text) >= max_chars:
                buf.append(text[: max_chars - total])
                total = max_chars
                truncated = True
                break
            buf.append(text)
            total += len(text)
            buf.append("\n\n---\n\n")
            total += 7

        markdown = "".join(buf).rstrip()
        if not markdown.strip():
            return {"ok": False, "error": "empty_scanned"}

        return {
            "ok": True,
            "markdown": markdown,
            "pages": len(pages),
            "chars": len(markdown),
            "truncated": truncated,
            "extractor": "pypdf",
        }
    except Exception:
        log.exception("pypdf extraction failed for %s", path)
        return {"ok": False, "error": "parse_error"}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/nimo/nimoos/NimoOS-AI/agent && python -m pytest tests/test_attachments_extract.py -v`
Expected: PASS for all PDF tests (and the prior two).

- [ ] **Step 6: Commit**

```bash
cd /home/nimo/nimoos/NimoOS-AI
git add agent/attachments/extract.py agent/tests/test_attachments_extract.py agent/tests/fixtures/tiny.pdf
git commit -m "$(cat <<'EOF'
feat(agent): pdf extractor in attachments.extract

Uses pypdf with encryption short-circuit, empty-after-strip detection
(scanned PDFs), per-page exception isolation, and a top-level broad
except envelope. Truncates to max_chars at extract time.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: DOCX extractor (`python-docx`)

**Files:**
- Modify: `agent/attachments/extract.py` (the `_extract_docx` function + a new shared `_zipbomb_check` helper)
- Modify: `agent/tests/test_attachments_extract.py`

DOCX is the first ZIP-based format; this task introduces the shared `_zipbomb_check(path, cap)` helper that the XLSX/PPTX tasks will reuse.

- [ ] **Step 1: Write failing tests for the DOCX extractor**

Append to `agent/tests/test_attachments_extract.py`:

```python
def _build_docx(tmp_path, paragraphs):
    """Build an in-memory .docx using python-docx and return its path."""
    import docx
    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    p = tmp_path / "doc.docx"
    d.save(str(p))
    return str(p)


def test_docx_happy_path(tmp_path):
    from attachments.extract import extract_to_markdown
    path = _build_docx(tmp_path, ["First paragraph.", "Second paragraph."])
    result = extract_to_markdown(path, "docx",
                                 max_chars=10_000,
                                 max_uncompressed_bytes=10_000_000)
    assert result["ok"] is True
    assert "First paragraph." in result["markdown"]
    assert "Second paragraph." in result["markdown"]
    assert result["extractor"] == "python-docx"
    assert result["truncated"] is False


def test_docx_truncation(tmp_path):
    from attachments.extract import extract_to_markdown
    big = "x" * 500
    path = _build_docx(tmp_path, [big, big, big])
    result = extract_to_markdown(path, "docx",
                                 max_chars=100,
                                 max_uncompressed_bytes=10_000_000)
    assert result["ok"] is True
    assert result["truncated"] is True
    assert len(result["markdown"]) <= 100


def test_docx_garbage_returns_parse_error(tmp_path):
    from attachments.extract import extract_to_markdown
    p = tmp_path / "junk.docx"
    p.write_bytes(b"not a zip at all")
    result = extract_to_markdown(str(p), "docx",
                                 max_chars=1000,
                                 max_uncompressed_bytes=10_000_000)
    assert result == {"ok": False, "error": "parse_error"}


def test_docx_zip_bomb_precheck(tmp_path):
    """A ZIP whose summed uncompressed size exceeds the cap is rejected
    before python-docx ever touches it."""
    import zipfile
    from attachments.extract import extract_to_markdown
    p = tmp_path / "bomb.docx"
    # Two entries totalling ~3000 bytes uncompressed; cap of 1000 rejects.
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("a.xml", "A" * 2000)
        z.writestr("b.xml", "B" * 1000)
    result = extract_to_markdown(str(p), "docx",
                                 max_chars=10_000,
                                 max_uncompressed_bytes=1000)
    assert result == {"ok": False, "error": "zip_bomb"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/nimo/nimoos/NimoOS-AI/agent && python -m pytest tests/test_attachments_extract.py -v`
Expected: the 4 new docx tests FAIL.

- [ ] **Step 3: Add the shared `_zipbomb_check` helper and implement `_extract_docx`**

Edit `agent/attachments/extract.py`. Below `log = logging.getLogger(__name__)`, add:

```python
def _zipbomb_check(path: str, max_uncompressed_bytes: int) -> bool:
    """True if the ZIP at `path` decompresses within budget. False otherwise.
    A non-ZIP (or unreadable ZIP) also returns False — caller treats that as
    a zip_bomb decision rather than crashing; the subsequent parser call will
    typically report parse_error if invoked, but for ZIP-based formats we
    bail early."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            total = sum(zi.file_size for zi in z.infolist())
    except (zipfile.BadZipFile, OSError):
        return False
    return total <= max_uncompressed_bytes
```

Replace the `_extract_docx` stub body:

```python
def _extract_docx(path: str, *, max_chars: int,
                  max_uncompressed_bytes: int) -> dict:
    try:
        import docx  # python-docx
    except ImportError:
        return {"ok": False, "error": "not_installed"}

    if not _zipbomb_check(path, max_uncompressed_bytes):
        return {"ok": False, "error": "zip_bomb"}

    try:
        d = docx.Document(path)
        buf: list[str] = []
        total = 0
        truncated = False

        def emit(line: str) -> bool:
            """Append `line + \\n`; return True if we hit the cap."""
            nonlocal total, truncated
            if total + len(line) + 1 >= max_chars:
                buf.append(line[: max_chars - total])
                total = max_chars
                truncated = True
                return True
            buf.append(line + "\n")
            total += len(line) + 1
            return False

        for para in d.paragraphs:
            text = (para.text or "").strip()
            if not text:
                continue
            style = (para.style.name or "") if para.style is not None else ""
            if style.startswith("Heading"):
                # "Heading 1" → "# ", "Heading 2" → "## ", etc.
                try:
                    level = int(style.split()[-1])
                except ValueError:
                    level = 1
                level = max(1, min(level, 6))
                if emit("#" * level + " " + text):
                    break
            else:
                if emit(text):
                    break

        if not truncated:
            for table in d.tables:
                if truncated:
                    break
                rows = []
                for row in table.rows:
                    cells = [(cell.text or "").replace("|", "\\|").replace("\n", " ")
                             for cell in row.cells]
                    rows.append("| " + " | ".join(cells) + " |")
                if not rows:
                    continue
                header = rows[0]
                divider = "| " + " | ".join(["---"] * header.count("|") if header.count("|") > 1 else ["---"]) + " |"
                if emit(""): break
                if emit(header): break
                if emit(divider): break
                for body_row in rows[1:]:
                    if emit(body_row):
                        break

        markdown = "".join(buf).rstrip()
        if not markdown.strip():
            return {"ok": False, "error": "empty_scanned"}

        return {
            "ok": True,
            "markdown": markdown,
            "pages": None,
            "chars": len(markdown),
            "truncated": truncated,
            "extractor": "python-docx",
        }
    except Exception:
        log.exception("python-docx extraction failed for %s", path)
        return {"ok": False, "error": "parse_error"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/nimo/nimoos/NimoOS-AI/agent && python -m pytest tests/test_attachments_extract.py -v`
Expected: PASS for all (docx + earlier).

- [ ] **Step 5: Commit**

```bash
cd /home/nimo/nimoos/NimoOS-AI
git add agent/attachments/extract.py agent/tests/test_attachments_extract.py
git commit -m "$(cat <<'EOF'
feat(agent): docx extractor + zip-bomb precheck helper

python-docx with paragraphs (preserving heading level via style.name) and
tables (rendered as pipe-tables). New _zipbomb_check sums infolist()
file_size and is reused by xlsx/pptx in subsequent commits.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: XLSX extractor (`openpyxl`, with `data_only` fallback)

**Files:**
- Modify: `agent/attachments/extract.py` (the `_extract_xlsx` function)
- Modify: `agent/tests/test_attachments_extract.py`

Two-pass strategy: first open `read_only=True, data_only=True`. If a sheet produces only `None` values despite `max_row > 0`, re-open `data_only=False` for that workbook and emit formula text. The fallback path is rare but high-value (Python-generated xlsx files).

- [ ] **Step 1: Write failing tests for the XLSX extractor**

Append to `agent/tests/test_attachments_extract.py`:

```python
def _build_xlsx(tmp_path, sheets):
    """sheets: list of (title, list-of-rows). Returns path."""
    import openpyxl
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets:
        ws = wb.create_sheet(title=title)
        for row in rows:
            ws.append(row)
    p = tmp_path / "book.xlsx"
    wb.save(str(p))
    return str(p)


def test_xlsx_happy_path(tmp_path):
    from attachments.extract import extract_to_markdown
    path = _build_xlsx(tmp_path, [
        ("Sheet1", [["Name", "Age"], ["Alice", 30], ["Bob", 25]]),
    ])
    result = extract_to_markdown(path, "xlsx",
                                 max_chars=10_000,
                                 max_uncompressed_bytes=10_000_000)
    assert result["ok"] is True
    assert "## Sheet1" in result["markdown"]
    assert "Alice" in result["markdown"]
    assert "Bob" in result["markdown"]
    assert result["pages"] == 1  # sheet count
    assert result["extractor"] == "openpyxl"


def test_xlsx_multiple_sheets(tmp_path):
    from attachments.extract import extract_to_markdown
    path = _build_xlsx(tmp_path, [
        ("Numbers", [[1, 2], [3, 4]]),
        ("Letters", [["a", "b"], ["c", "d"]]),
    ])
    result = extract_to_markdown(path, "xlsx",
                                 max_chars=10_000,
                                 max_uncompressed_bytes=10_000_000)
    assert result["ok"] is True
    assert "## Numbers" in result["markdown"]
    assert "## Letters" in result["markdown"]
    assert result["pages"] == 2


def test_xlsx_data_only_falls_back_to_formula(tmp_path):
    """A workbook whose cells are all formulas with no cached calc values
    (the canonical Python-generated case) triggers the data_only=False
    fallback so the model gets formula text instead of all-None rows."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Calc"
    # Formulas only — openpyxl never writes a cached <v>, so data_only=True
    # returns None for every cell.
    ws["A1"] = "=1+1"
    ws["A2"] = "=2+2"
    p = tmp_path / "formula.xlsx"
    wb.save(str(p))

    from attachments.extract import extract_to_markdown
    result = extract_to_markdown(str(p), "xlsx",
                                 max_chars=10_000,
                                 max_uncompressed_bytes=10_000_000)
    assert result["ok"] is True
    # Fallback path emits the formula text rather than blank cells.
    assert "=1+1" in result["markdown"]
    assert "=2+2" in result["markdown"]


def test_xlsx_zip_bomb_precheck(tmp_path):
    import zipfile
    from attachments.extract import extract_to_markdown
    p = tmp_path / "bomb.xlsx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("a.xml", "A" * 5000)
    result = extract_to_markdown(str(p), "xlsx",
                                 max_chars=10_000,
                                 max_uncompressed_bytes=1000)
    assert result == {"ok": False, "error": "zip_bomb"}


def test_xlsx_garbage_returns_parse_error(tmp_path):
    from attachments.extract import extract_to_markdown
    p = tmp_path / "junk.xlsx"
    p.write_bytes(b"not a zip")
    result = extract_to_markdown(str(p), "xlsx",
                                 max_chars=1000,
                                 max_uncompressed_bytes=10_000_000)
    assert result == {"ok": False, "error": "parse_error"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/nimo/nimoos/NimoOS-AI/agent && python -m pytest tests/test_attachments_extract.py -v`
Expected: the 5 new xlsx tests FAIL.

- [ ] **Step 3: Implement `_extract_xlsx`**

Replace the `_extract_xlsx` stub body in `agent/attachments/extract.py`:

```python
def _extract_xlsx(path: str, *, max_chars: int,
                  max_uncompressed_bytes: int) -> dict:
    try:
        import openpyxl
    except ImportError:
        return {"ok": False, "error": "not_installed"}

    if not _zipbomb_check(path, max_uncompressed_bytes):
        return {"ok": False, "error": "zip_bomb"}

    def _emit_sheet(ws, buf, total, truncated):
        """Append one sheet's markdown to buf. Returns
        (new_total, truncated, saw_any_value)."""
        saw_any_value = False
        rows_iter = ws.iter_rows(values_only=True)
        first_row = None
        for row in rows_iter:
            first_row = row
            break
        if first_row is None:
            return total, truncated, saw_any_value

        def cell_str(v):
            nonlocal saw_any_value
            if v is not None:
                saw_any_value = True
            return "" if v is None else str(v).replace("|", "\\|").replace("\n", " ")

        def append(line):
            nonlocal total, truncated
            if total + len(line) + 1 >= max_chars:
                buf.append(line[: max_chars - total])
                total = max_chars
                truncated = True
                return True
            buf.append(line + "\n")
            total += len(line) + 1
            return False

        if append(f"## {ws.title}"):
            return total, truncated, saw_any_value
        if append(""):
            return total, truncated, saw_any_value

        header_cells = [cell_str(v) for v in first_row]
        header_line = "| " + " | ".join(header_cells) + " |"
        divider_line = "| " + " | ".join(["---"] * len(header_cells)) + " |"
        if append(header_line):
            return total, truncated, saw_any_value
        if append(divider_line):
            return total, truncated, saw_any_value
        for row in rows_iter:
            body_cells = [cell_str(v) for v in row]
            if append("| " + " | ".join(body_cells) + " |"):
                return total, truncated, saw_any_value
        if append(""):
            return total, truncated, saw_any_value
        return total, truncated, saw_any_value

    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        sheet_count = len(wb.sheetnames)
        buf: list[str] = []
        total = 0
        truncated = False
        any_value_seen = False
        for ws in wb.worksheets:
            if truncated:
                break
            total, truncated, saw_any = _emit_sheet(ws, buf, total, truncated)
            any_value_seen = any_value_seen or saw_any
        wb.close()

        # Fallback: if every cell came back None (Python-generated xlsx with
        # no cached calc values), re-open with data_only=False to surface
        # formula text.
        if not any_value_seen and sheet_count > 0:
            buf = []
            total = 0
            truncated = False
            wb2 = openpyxl.load_workbook(path, read_only=True, data_only=False)
            for ws in wb2.worksheets:
                if truncated:
                    break
                total, truncated, _ = _emit_sheet(ws, buf, total, truncated)
            wb2.close()

        markdown = "".join(buf).rstrip()
        if not markdown.strip():
            return {"ok": False, "error": "empty_scanned"}

        return {
            "ok": True,
            "markdown": markdown,
            "pages": sheet_count,
            "chars": len(markdown),
            "truncated": truncated,
            "extractor": "openpyxl",
        }
    except Exception:
        log.exception("openpyxl extraction failed for %s", path)
        return {"ok": False, "error": "parse_error"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/nimo/nimoos/NimoOS-AI/agent && python -m pytest tests/test_attachments_extract.py -v`
Expected: PASS for all.

- [ ] **Step 5: Commit**

```bash
cd /home/nimo/nimoos/NimoOS-AI
git add agent/attachments/extract.py agent/tests/test_attachments_extract.py
git commit -m "$(cat <<'EOF'
feat(agent): xlsx extractor with data_only empty-cache fallback

openpyxl with read_only=True, data_only=True. When a workbook has no
cached calc values (common with Python-generated xlsx), falls back to
data_only=False so the model gets formula text instead of all-blank.
Reuses _zipbomb_check.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: PPTX extractor (`python-pptx`)

**Files:**
- Modify: `agent/attachments/extract.py` (the `_extract_pptx` function)
- Modify: `agent/tests/test_attachments_extract.py`

- [ ] **Step 1: Write failing tests for the PPTX extractor**

Append to `agent/tests/test_attachments_extract.py`:

```python
def _build_pptx(tmp_path, slides_text):
    """slides_text: list[list[str]] — per slide, list of paragraphs."""
    from pptx import Presentation
    p = Presentation()
    layout = p.slide_layouts[5]  # title only
    for paragraphs in slides_text:
        slide = p.slides.add_slide(layout)
        title = slide.shapes.title
        title.text = paragraphs[0] if paragraphs else ""
        if len(paragraphs) > 1:
            txbox = slide.shapes.add_textbox(left=0, top=0, width=100, height=100)
            tf = txbox.text_frame
            tf.text = paragraphs[1]
            for extra in paragraphs[2:]:
                tf.add_paragraph().text = extra
    out = tmp_path / "deck.pptx"
    p.save(str(out))
    return str(out)


def test_pptx_happy_path(tmp_path):
    from attachments.extract import extract_to_markdown
    path = _build_pptx(tmp_path, [
        ["Title One", "Body of slide 1"],
        ["Title Two", "Body of slide 2", "Another paragraph"],
    ])
    result = extract_to_markdown(path, "pptx",
                                 max_chars=10_000,
                                 max_uncompressed_bytes=10_000_000)
    assert result["ok"] is True
    assert "Title One" in result["markdown"]
    assert "Title Two" in result["markdown"]
    assert "Body of slide 1" in result["markdown"]
    assert result["pages"] == 2
    assert result["extractor"] == "python-pptx"


def test_pptx_truncation(tmp_path):
    from attachments.extract import extract_to_markdown
    path = _build_pptx(tmp_path, [["T", "x" * 5000]] * 3)
    result = extract_to_markdown(path, "pptx",
                                 max_chars=200,
                                 max_uncompressed_bytes=10_000_000)
    assert result["ok"] is True
    assert result["truncated"] is True
    assert len(result["markdown"]) <= 200


def test_pptx_zip_bomb_precheck(tmp_path):
    import zipfile
    from attachments.extract import extract_to_markdown
    p = tmp_path / "bomb.pptx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("a.xml", "A" * 5000)
    result = extract_to_markdown(str(p), "pptx",
                                 max_chars=10_000,
                                 max_uncompressed_bytes=1000)
    assert result == {"ok": False, "error": "zip_bomb"}


def test_pptx_garbage_returns_parse_error(tmp_path):
    from attachments.extract import extract_to_markdown
    p = tmp_path / "junk.pptx"
    p.write_bytes(b"not a zip")
    result = extract_to_markdown(str(p), "pptx",
                                 max_chars=1000,
                                 max_uncompressed_bytes=10_000_000)
    assert result == {"ok": False, "error": "parse_error"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/nimo/nimoos/NimoOS-AI/agent && python -m pytest tests/test_attachments_extract.py -v`
Expected: the 4 new pptx tests FAIL.

- [ ] **Step 3: Implement `_extract_pptx`**

Replace the `_extract_pptx` stub body in `agent/attachments/extract.py`:

```python
def _extract_pptx(path: str, *, max_chars: int,
                  max_uncompressed_bytes: int) -> dict:
    try:
        from pptx import Presentation
    except ImportError:
        return {"ok": False, "error": "not_installed"}

    if not _zipbomb_check(path, max_uncompressed_bytes):
        return {"ok": False, "error": "zip_bomb"}

    try:
        deck = Presentation(path)
        slides = list(deck.slides)
        buf: list[str] = []
        total = 0
        truncated = False

        def emit(line: str) -> bool:
            nonlocal total, truncated
            if total + len(line) + 1 >= max_chars:
                buf.append(line[: max_chars - total])
                total = max_chars
                truncated = True
                return True
            buf.append(line + "\n")
            total += len(line) + 1
            return False

        for i, slide in enumerate(slides, start=1):
            if truncated:
                break
            if emit(f"## Slide {i}"):
                break
            for shape in slide.shapes:
                if truncated:
                    break
                if not getattr(shape, "has_text_frame", False):
                    continue
                for para in shape.text_frame.paragraphs:
                    text = "".join(run.text for run in para.runs).strip()
                    if not text:
                        continue
                    if emit(f"- {text}"):
                        break
            if not truncated:
                if emit(""):
                    break

        markdown = "".join(buf).rstrip()
        body_lines = [
            line for line in markdown.splitlines()
            if line.strip() and not line.startswith("## Slide ")
        ]
        if not body_lines:
            # All we got was slide headers and no body text — treat as empty.
            return {"ok": False, "error": "empty_scanned"}

        return {
            "ok": True,
            "markdown": markdown,
            "pages": len(slides),
            "chars": len(markdown),
            "truncated": truncated,
            "extractor": "python-pptx",
        }
    except Exception:
        log.exception("python-pptx extraction failed for %s", path)
        return {"ok": False, "error": "parse_error"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/nimo/nimoos/NimoOS-AI/agent && python -m pytest tests/test_attachments_extract.py -v`
Expected: PASS for all (16 cases total: 2 dispatcher + 6 pdf + 4 docx + 5 xlsx + 4 pptx — adjust if counted differently).

- [ ] **Step 5: Commit**

```bash
cd /home/nimo/nimoos/NimoOS-AI
git add agent/attachments/extract.py agent/tests/test_attachments_extract.py
git commit -m "$(cat <<'EOF'
feat(agent): pptx extractor in attachments.extract

python-pptx with per-slide title + bullet body. Reuses _zipbomb_check
and the standard broad-except envelope. Header-only output (no body
text on any slide) is treated as empty_scanned.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Add config knobs in `main.py`

**Files:**
- Modify: `agent/main.py`

The three new env-driven constants need to exist before Task 8 plumbs them through. No tests — they're simple constants, exercised by later integration tests.

- [ ] **Step 1: Add the three constants**

Edit `agent/main.py`. After the `FFPROBE_TIMEOUT` line (currently around line 97), add:

```python
MAX_DOC_CHARS               = _env_int("NIMOOS_MAX_DOC_CHARS",               262_144)
MAX_DOC_EXTRACT_SECONDS     = _env_int("NIMOOS_MAX_DOC_EXTRACT_SECONDS",     8)
MAX_DOC_UNCOMPRESSED_BYTES  = _env_int("NIMOOS_MAX_DOC_UNCOMPRESSED_BYTES",  209_715_200)
```

- [ ] **Step 2: Smoke-import main.py to verify no syntax errors**

Run: `cd /home/nimo/nimoos/NimoOS-AI/agent && python -c "import main; print(main.MAX_DOC_CHARS, main.MAX_DOC_EXTRACT_SECONDS, main.MAX_DOC_UNCOMPRESSED_BYTES)"`
Expected: prints `262144 8 209715200`.

- [ ] **Step 3: Commit**

```bash
cd /home/nimo/nimoos/NimoOS-AI
git add agent/main.py
git commit -m "$(cat <<'EOF'
feat(agent): config knobs for document extraction limits

NIMOOS_MAX_DOC_CHARS / _EXTRACT_SECONDS / _UNCOMPRESSED_BYTES, env-driven
following the existing _env_int pattern. Plumbed through to handle_upload
in a subsequent commit.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Make `handle_upload` async; add `document` branch with sidecar + timeout

**Files:**
- Modify: `agent/attachments/upload.py`
- Modify: `agent/main.py` (await call site, pass new kwargs)
- Create: `agent/tests/test_attachments_upload_document.py`

This is the largest task. We're changing `handle_upload` from sync to async, plumbing three new kwargs, inserting the `document` branch (with `asyncio.to_thread` + `wait_for` timeout, sidecar write, failure-keep-document policy, sidecar-write graceful degrade, and DB-failure cleanup including the sidecar).

The endpoint at `main.py:304` is the only caller (`grep -rn "handle_upload"` confirmed). It needs `await` and the three new kwargs.

- [ ] **Step 1: Write failing tests for the document branch**

Create `agent/tests/test_attachments_upload_document.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/nimo/nimoos/NimoOS-AI/agent && python -m pytest tests/test_attachments_upload_document.py -v`
Expected: all 5 tests FAIL (upload returns kind=binary because the document branch doesn't exist yet).

- [ ] **Step 3: Convert `handle_upload` to async + add the document branch + sidecar helper**

Edit `agent/attachments/upload.py`. Add `import asyncio` at the top. Add `from . import extract` import.

Add a small module-level helper that the test monkeypatches:

```python
def _write_sidecar(path: str, content: str) -> None:
    """Write `content` to `path` as UTF-8. Raises OSError on failure;
    callers in handle_upload catch that for graceful degradation."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
```

Replace the entire `handle_upload` function with:

```python
async def handle_upload(*, conn: sqlite3.Connection, data_root: str,
                        session_id: str, original_name: str, part_path: str,
                        size: int, max_image_size: int,
                        ffprobe_timeout: float,
                        max_doc_chars: int,
                        max_doc_uncompressed_bytes: int,
                        max_extract_seconds: float) -> dict:
    """Given a successfully streamed .part file, classify, run any
    format-specific extraction (ffprobe for av, document extractor for
    office files), rename to final, write any sidecar, and INSERT the row."""
    mime, kind = classify(part_path, original_name)

    if kind == "image" and size > max_image_size:
        _safe_unlink(part_path)
        raise HTTPException(status_code=413,
                            detail="image exceeds MaxImageAttachmentSize")

    meta: dict | None = None
    extracted_markdown: str | None = None

    if kind in ("video", "audio"):
        result = probe(part_path, timeout=ffprobe_timeout)
        if result["ok"]:
            meta = {k: v for k, v in result.items() if k != "ok"}
        else:
            kind = "binary"
            meta = {"ffprobe_error": result["error"]}

    elif kind == "document":
        ext = os.path.splitext(original_name)[1].lstrip(".").lower()
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    extract.extract_to_markdown,
                    part_path, ext,
                    max_chars=max_doc_chars,
                    max_uncompressed_bytes=max_doc_uncompressed_bytes,
                ),
                timeout=max_extract_seconds,
            )
        except asyncio.TimeoutError:
            result = {"ok": False, "error": "timeout"}

        if result["ok"]:
            extracted_markdown = result["markdown"]
            meta = {
                "extractor": result["extractor"],
                "pages":     result["pages"],
                "chars":     result["chars"],
                "truncated": result["truncated"],
                # "sidecar" added after rename succeeds, see below
            }
        elif result["error"] == "not_installed":
            kind = "binary"
            meta = {"extract_error": "not_installed"}
        else:
            # kind stays "document" so the model can explain the error.
            meta = {"extract_error": result["error"]}

    aid = "att_" + uuid.uuid4().hex[:12]
    sanitized = sanitize_filename(original_name)
    rel_name = f"{aid}__{sanitized}"
    final_dest = os.path.join(os.path.dirname(part_path), rel_name)

    try:
        os.rename(part_path, final_dest)
    except OSError as e:
        _safe_unlink(part_path)
        raise HTTPException(status_code=500, detail=f"rename: {e}")

    sidecar_path: str | None = None
    if extracted_markdown is not None:
        sidecar_name = rel_name + ".md"
        sidecar_path = os.path.join(os.path.dirname(part_path), sidecar_name)
        try:
            _write_sidecar(sidecar_path, extracted_markdown)
            meta["sidecar"] = sidecar_name
        except OSError:
            # Graceful degradation: original file remains; surface the failure
            # via extract_error so the model can tell the user.
            sidecar_path = None
            meta = {"extract_error": "sidecar_write_failed"}

    now = int(time.time())
    try:
        conn.execute(
            "INSERT INTO attachments "
            "(id,session_id,message_id,filename,mime,kind,size_bytes,rel_path,"
            " meta_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (aid, session_id, None, original_name, mime, kind, size,
             rel_name, json.dumps(meta) if meta else None, now),
        )
        conn.commit()
    except sqlite3.Error as e:
        _safe_unlink(final_dest)
        if sidecar_path is not None:
            _safe_unlink(sidecar_path)
        raise HTTPException(status_code=500, detail=f"db: {e}")

    return {
        "id": aid,
        "filename": original_name,
        "mime": mime,
        "kind": kind,
        "size_bytes": size,
        "created_at": now,
        "meta": meta,
    }
```

- [ ] **Step 4: Update the call site in `main.py`**

Edit `agent/main.py`. Change the `upload_attachment` endpoint's call to `handle_upload`:

```python
    result = await att_upload.handle_upload(
        conn=conn,
        data_root=_data_root(),
        session_id=session_id,
        original_name=file.filename or "untitled",
        part_path=part_path,
        size=size,
        max_image_size=MAX_IMAGE_ATTACHMENT_SIZE,
        ffprobe_timeout=FFPROBE_TIMEOUT,
        max_doc_chars=MAX_DOC_CHARS,
        max_doc_uncompressed_bytes=MAX_DOC_UNCOMPRESSED_BYTES,
        max_extract_seconds=MAX_DOC_EXTRACT_SECONDS,
    )
```

(Only changes: prefix with `await`, and add the three new kwargs.)

- [ ] **Step 5: Run the new tests + existing upload tests**

Run: `cd /home/nimo/nimoos/NimoOS-AI/agent && python -m pytest tests/test_attachments_upload_document.py tests/test_attachments_upload.py tests/test_attachments_ffprobe.py -v`
Expected: PASS for all new tests; existing tests still pass (no behavioral change for image/text/binary/video/audio).

- [ ] **Step 6: Commit**

```bash
cd /home/nimo/nimoos/NimoOS-AI
git add agent/attachments/upload.py agent/main.py agent/tests/test_attachments_upload_document.py
git commit -m "$(cat <<'EOF'
feat(agent): document branch in handle_upload (async + threadpool + timeout)

handle_upload becomes async; calls extract_to_markdown via
asyncio.wait_for(asyncio.to_thread(...)) so CPU-bound parsing never
blocks the event loop. On extraction failure the row keeps kind=document
with meta.extract_error so the model can explain to the user; only
not_installed downgrades to binary. Sidecar write failure is also a
graceful degradation, not a 500.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: `read_attachment` document branch

**Files:**
- Modify: `agent/skills/attachments.py`
- Modify: `agent/tests/test_read_attachment_tool.py`

Two sub-paths under `kind=document`:
- `meta.extract_error` set → return `{"kind":"document","error":..., filename, mime}` so the model can translate to user-facing prose.
- Otherwise → read the `meta.sidecar` file, return content (truncated to `max_chars`).

- [ ] **Step 1: Write failing tests for the document branch**

Append to `agent/tests/test_read_attachment_tool.py`:

```python
def test_document_with_sidecar_returns_content(setup, tmp_path):
    conn, root, skill = setup
    # Original file (bytes don't matter for read_attachment in document path,
    # but we keep one on disk to mirror real upload state).
    _mk_att(conn, root, aid="d1", kind="document", mime="application/pdf",
            content_bytes=b"%PDF-1.4\n", filename="r.pdf",
            meta={"sidecar": "d1__r.pdf.md", "extractor": "pypdf",
                  "pages": 4, "chars": 12, "truncated": False})
    # Drop sidecar next to the original.
    side = root / "sessions" / "s1" / "attachments" / "d1__r.pdf.md"
    side.write_text("# Title\nbody text", encoding="utf-8")

    result = skill._read_attachment_impl(
        "d1", session_id="s1", user_id="u1", max_chars=1000,
        conn=conn, data_root=str(root))
    assert result["kind"] == "document"
    assert result["content"] == "# Title\nbody text"
    assert result["extractor"] == "pypdf"
    assert result["pages"] == 4
    assert result["truncated"] is False


def test_document_with_extract_error_returns_error_field(setup):
    conn, root, skill = setup
    _mk_att(conn, root, aid="d2", kind="document", mime="application/pdf",
            content_bytes=b"%PDF-1.4\n", filename="scan.pdf",
            meta={"extract_error": "empty_scanned"})
    result = skill._read_attachment_impl(
        "d2", session_id="s1", user_id="u1", max_chars=1000,
        conn=conn, data_root=str(root))
    assert result == {"kind": "document", "filename": "scan.pdf",
                      "mime": "application/pdf", "error": "empty_scanned",
                      "total_bytes": len(b"%PDF-1.4\n")}


def test_document_sidecar_missing_returns_vanished(setup):
    conn, root, skill = setup
    _mk_att(conn, root, aid="d3", kind="document", mime="application/pdf",
            content_bytes=b"%PDF-1.4\n", filename="r.pdf",
            meta={"sidecar": "d3__r.pdf.md", "extractor": "pypdf",
                  "pages": 1, "chars": 0, "truncated": False})
    # Sidecar deliberately NOT written.
    result = skill._read_attachment_impl(
        "d3", session_id="s1", user_id="u1", max_chars=1000,
        conn=conn, data_root=str(root))
    assert result == {"error": "vanished"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/nimo/nimoos/NimoOS-AI/agent && python -m pytest tests/test_read_attachment_tool.py -v`
Expected: the 3 new document tests FAIL (current implementation falls through to the binary branch).

- [ ] **Step 3: Add the `document` branch in `_read_attachment_impl`**

Edit `agent/skills/attachments.py`. After the existing `kind == "text"` branch and before the `kind in ("video", "audio")` branch, insert:

```python
    if kind == "document":
        meta = json.loads(meta_json) if meta_json else {}

        if "extract_error" in meta:
            return {
                "kind": "document",
                "filename": filename,
                "mime": mime,
                "error": meta["extract_error"],
                "total_bytes": size_bytes,
            }

        sidecar_name = meta.get("sidecar")
        if not sidecar_name:
            return {"error": "vanished"}
        sidecar_path = os.path.join(data_root, "sessions", session_id,
                                    "attachments", sidecar_name)
        if not os.path.exists(sidecar_path):
            return {"error": "vanished"}

        read_bytes = max_chars * 4
        with open(sidecar_path, "rb") as f:
            raw = f.read(read_bytes + 1)
        decoded = raw.decode("utf-8", errors="ignore")
        truncated = (len(decoded) > max_chars
                     or len(raw) > read_bytes
                     or bool(meta.get("truncated")))
        return {
            "kind": "document",
            "filename": filename,
            "mime": mime,
            "extractor": meta.get("extractor"),
            "pages": meta.get("pages"),
            "content": decoded[:max_chars],
            "truncated": truncated,
            "total_bytes": size_bytes,
        }
```

Also update the `@function_tool` docstring on `read_attachment`:

```python
@function_tool
def read_attachment(attachment_id: str) -> dict:
    """Read the contents of an attachment the user sent in this conversation.

    Use this when the user references a file (PDF, video, log, etc.) you don't
    already see inline. Image attachments are already visible — don't call this
    on them.

    For kind=document attachments, the response is either:
      • {"kind":"document", "content": "<markdown>", "extractor": ..., "pages": ...}
        — extracted text is available.
      • {"kind":"document", "error": "<reason>"} — extraction failed. Reasons:
        empty_scanned (likely scanned PDF; you don't have OCR),
        encrypted (password-protected PDF),
        zip_bomb (file rejected for size),
        timeout (too large/complex to extract in time),
        parse_error (corrupt or unsupported variant),
        sidecar_write_failed (transient server error).
        In all error cases, explain in the user's language what's wrong and
        suggest a remedy (e.g., share a text PDF, paste the content, retry).
    """
    ...  # unchanged body
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/nimo/nimoos/NimoOS-AI/agent && python -m pytest tests/test_read_attachment_tool.py -v`
Expected: PASS for all (including the 3 new ones).

- [ ] **Step 5: Commit**

```bash
cd /home/nimo/nimoos/NimoOS-AI
git add agent/skills/attachments.py agent/tests/test_read_attachment_tool.py
git commit -m "$(cat <<'EOF'
feat(agent): read_attachment document branch (sidecar + error path)

Returns {kind:"document", content:..., extractor, pages} on success or
{kind:"document", error:"<reason>"} when extraction failed at upload time.
Tool docstring teaches the model to translate the error codes into
user-facing language.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Update `attachment_system_block` so the model knows document errors may surface

**Files:**
- Modify: `agent/agent.py`

Small copy tweak. The current system block already lists non-image attachments and tells the model to call `read_attachment`. Add one line so the model is primed to handle the `error` field when present.

- [ ] **Step 1: Edit `attachment_system_block` in `agent/agent.py`**

Edit `agent/agent.py:273-287`. Replace the function with:

```python
def attachment_system_block(attachment_ids, *, session_id: str) -> str:
    """System-prompt suffix listing non-image attachments. Empty string when
    there are no non-image attachments."""
    rows = _fetch_attachments(attachment_ids, session_id)
    non_image = [r for r in rows if r["kind"] != "image"]
    if not non_image:
        return ""
    lines = ["The user attached the following files to their message:"]
    for r in non_image:
        size_kb = max(1, r["size_bytes"] // 1024)
        lines.append(f"- id={r['id']}, name=\"{r['filename']}\", "
                     f"kind={r['kind']}, size={size_kb} KB")
    lines.append("Use read_attachment(id) to inspect contents. "
                 "Image attachments are already visible — don't call this on them. "
                 "For kind=document, the response may include an `error` field "
                 "(e.g., empty_scanned, encrypted, timeout) — relay it to the user "
                 "in plain language in their own language.")
    return "\n".join(lines)
```

- [ ] **Step 2: Run agent-side tests to make sure nothing regresses**

Run: `cd /home/nimo/nimoos/NimoOS-AI/agent && python -m pytest tests/test_agent_attachment_injection.py -v`
Expected: PASS. (Existing tests check the structural shape; the additional sentence should not break them unless one asserts the exact closing string — verify and adjust assertions if so.)

If a test asserts the exact suffix string, update it to match the new text (e.g. by checking `"read_attachment"` substring inclusion instead of full-string equality).

- [ ] **Step 3: Commit**

```bash
cd /home/nimo/nimoos/NimoOS-AI
git add agent/agent.py agent/tests/test_agent_attachment_injection.py
git commit -m "$(cat <<'EOF'
feat(agent): system block primes model for document error responses

attachment_system_block now tells the model that kind=document
read_attachment responses may carry an `error` field, and to relay it
in plain user-facing language.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

(Drop `agent/tests/test_agent_attachment_injection.py` from the `git add` line if you didn't need to modify the test.)

---

## Task 11: Add the four extractor dependencies to `requirements.txt`

**Files:**
- Modify: `agent/requirements.txt`

All four are pure Python and install from PyPI on amd64 / arm64 / armv7 without native compilation.

- [ ] **Step 1: Append the new deps**

Edit `agent/requirements.txt`. The current contents end at `python-magic`. Append:

```
pypdf>=4.0
python-docx>=1.1
openpyxl>=3.1
python-pptx>=0.6
```

- [ ] **Step 2: Install and verify imports**

Run:

```bash
cd /home/nimo/nimoos/NimoOS-AI/agent
pip install -r requirements.txt
python -c "import pypdf, docx, openpyxl, pptx; print(pypdf.__version__, openpyxl.__version__)"
```

Expected: prints the installed versions without ImportError.

- [ ] **Step 3: Run the full new test surface end-to-end**

Run:

```bash
cd /home/nimo/nimoos/NimoOS-AI/agent
python -m pytest tests/test_attachments_kind.py tests/test_attachments_extract.py tests/test_attachments_upload_document.py tests/test_read_attachment_tool.py -v
```

Expected: all PASS. This is the closing-the-loop check — every layer of the new feature exercised with real libraries (Task 3–6 tests used fixtures that exercise the real extractors; Task 8's tests monkeypatch the extractor, which is fine because the extractor itself is independently covered).

- [ ] **Step 4: Commit**

```bash
cd /home/nimo/nimoos/NimoOS-AI
git add agent/requirements.txt
git commit -m "$(cat <<'EOF'
build(agent): add pypdf / python-docx / openpyxl / python-pptx

Pure-Python extractors for kind=document attachments. No system-level
deps required.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Final verification

After Task 11, run the full agent test suite once to catch any unintended regressions:

```bash
cd /home/nimo/nimoos/NimoOS-AI/agent
python -m pytest -v
```

Expected: all tests pass. If anything red, fix before declaring done.

Then build/deploy via the project's existing script:

```bash
bash /home/nimo/nimoos/nimo_os_docs/scripts/deploy.sh nimoos-ai
```

(Or whatever subcommand matches the AI service — check the script if unsure. Do NOT run this autonomously; ask the user first, since deploy writes to `/var/lib/nimoos/...` and restarts systemd units.)
