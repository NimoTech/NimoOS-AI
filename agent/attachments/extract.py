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
                raw = page.extract_text() or ""
            except Exception:
                # Per-page failure shouldn't sink the whole document; emit a
                # marker so the model knows that page was lost.
                raw = "[unreadable page]"
            text = raw.strip()
            if not text:
                continue
            if total + len(text) >= max_chars:
                buf.append(text[: max_chars - total])
                total = max_chars
                truncated = True
                break
            buf.append(text)
            total += len(text)
            buf.append("\n\n---\n\n")
            total += 7

        if not buf:
            return {"ok": False, "error": "empty_scanned"}

        markdown = "".join(buf).rstrip()

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
