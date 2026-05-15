import json
import os
import uuid
import time
import sqlite3
from fastapi import HTTPException, UploadFile

from .paths import sanitize_filename
from .kind import classify
from .ffprobe import probe


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


def handle_upload(*, conn: sqlite3.Connection, data_root: str,
                  session_id: str, original_name: str, part_path: str,
                  size: int, max_image_size: int,
                  ffprobe_timeout: float) -> dict:
    """Given a successfully streamed .part file at part_path, classify it,
    run ffprobe if needed, enforce image-specific size cap, then rename
    .part -> final and insert DB row. Returns the response dict."""
    mime, kind = classify(part_path, original_name)

    if kind == "image" and size > max_image_size:
        _safe_unlink(part_path)
        raise HTTPException(status_code=413,
                            detail="image exceeds MaxImageAttachmentSize")

    meta = None
    if kind in ("video", "audio"):
        result = probe(part_path, timeout=ffprobe_timeout)
        if result["ok"]:
            meta = {k: v for k, v in result.items() if k != "ok"}
        else:
            kind = "binary"
            meta = {"ffprobe_error": result["error"]}

    aid = "att_" + uuid.uuid4().hex[:12]
    sanitized = sanitize_filename(original_name)
    rel_name = f"{aid}__{sanitized}"
    final_dest = os.path.join(os.path.dirname(part_path), rel_name)

    try:
        os.rename(part_path, final_dest)
    except OSError as e:
        _safe_unlink(part_path)
        raise HTTPException(status_code=500, detail=f"rename: {e}")

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
