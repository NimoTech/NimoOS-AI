import asyncio
import concurrent.futures
import json
import os
import uuid
import time
import sqlite3
from fastapi import HTTPException, UploadFile

from .paths import sanitize_filename
from .kind import classify
from .ffprobe import probe
from . import extract


_CHUNK = 1 << 20  # 1 MiB


async def stream_to_disk(upload: UploadFile, dest_part: str,
                         limit_bytes: int) -> int:
    """Stream upload.file to dest_part, abort with HTTPException(413) if
    written bytes would exceed limit_bytes. Deletes the .part file on any
    exception (including 413) before re-raising. Returns total bytes written."""
    written = 0
    os.makedirs(os.path.dirname(dest_part), exist_ok=True)
    try:
        with open(dest_part, "wb") as out:
            while True:
                chunk = await upload.read(_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit_bytes:
                    raise HTTPException(status_code=413,
                                        detail="exceeds MaxAttachmentSize")
                out.write(chunk)
        return written
    except Exception:
        _safe_unlink(dest_part)
        raise


def _safe_unlink(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _write_sidecar(path: str, content: str) -> None:
    """Write `content` to `path` as UTF-8. Raises OSError on failure;
    callers in handle_upload catch that for graceful degradation."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


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
            loop = asyncio.get_event_loop()
            # Use a non-blocking executor submit (no `with` block so the
            # ThreadPoolExecutor.__exit__ does not block on thread completion
            # when wait_for times out).
            _pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = loop.run_in_executor(
                _pool,
                lambda: extract.extract_to_markdown(
                    part_path, ext,
                    max_chars=max_doc_chars,
                    max_uncompressed_bytes=max_doc_uncompressed_bytes,
                ),
            )
            result = await asyncio.wait_for(future, timeout=max_extract_seconds)
        except asyncio.TimeoutError:
            result = {"ok": False, "error": "timeout"}
        finally:
            # Allow threads to finish in background; don't block.
            _pool.shutdown(wait=False)

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
