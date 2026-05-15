"""read_attachment tool for the Agent loop.

Conditional tool — injected by AgentRunner only when the current run has
at least one non-image attachment (see Task 11). Reads attachment content
by kind:
- text → UTF-8 content, truncated to MAX_CHARS_VAR characters
- video/audio → ffprobe metadata only (no bytes)
- binary → human-readable note that bytes aren't returnable
- image → error pointing the model at the inline image block

Cross-session reads are rejected via (session_id, user_id) join.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextvars import ContextVar

from agents import function_tool


SESSION_ID_VAR: ContextVar[str] = ContextVar("att_session_id", default="")
USER_ID_VAR: ContextVar[str] = ContextVar("att_user_id", default="")
DB_VAR: ContextVar = ContextVar("att_db", default=None)
DATA_ROOT_VAR: ContextVar[str] = ContextVar("att_data_root", default="")
MAX_CHARS_VAR: ContextVar[int] = ContextVar("att_max_chars", default=32_768)


def _read_attachment_impl(attachment_id: str, *, session_id: str,
                          user_id: str, max_chars: int,
                          conn=None, data_root: str | None = None) -> dict:
    """Pure function under test. The @function_tool wrapper below reads
    its arguments from ContextVars set by AgentRunner at run start."""
    if conn is None:
        conn = DB_VAR.get()
        if conn is None:
            import db as db_module
            conn = db_module.get_connection()
    if data_root is None:
        data_root = DATA_ROOT_VAR.get() or ""

    row = conn.execute(
        "SELECT a.filename, a.mime, a.kind, a.size_bytes, a.rel_path, "
        "       a.meta_json "
        "FROM attachments a JOIN sessions s ON s.id = a.session_id "
        "WHERE a.id = ? AND a.session_id = ? AND s.user_id = ?",
        (attachment_id, session_id, user_id),
    ).fetchone()
    if row is None:
        return {"error": "not_found"}

    # row may be sqlite3.Row or tuple
    if isinstance(row, sqlite3.Row):
        filename = row["filename"]
        mime = row["mime"]
        kind = row["kind"]
        size_bytes = row["size_bytes"]
        rel_path = row["rel_path"]
        meta_json = row["meta_json"]
    else:
        filename, mime, kind, size_bytes, rel_path, meta_json = row

    if kind == "image":
        return {"error": "image_already_visible", "filename": filename}

    full = os.path.join(data_root, "sessions", session_id, "attachments",
                        rel_path)

    if kind == "text":
        if not os.path.exists(full):
            return {"error": "vanished"}
        read_bytes = max_chars * 4
        with open(full, "rb") as f:
            raw = f.read(read_bytes + 1)
        decoded = raw.decode("utf-8", errors="ignore")
        truncated = len(decoded) > max_chars or len(raw) > read_bytes
        return {
            "kind": "text",
            "filename": filename,
            "content": decoded[:max_chars],
            "truncated": truncated,
            "total_bytes": size_bytes,
        }

    if kind in ("video", "audio"):
        if not os.path.exists(full):
            return {"error": "vanished"}
        return {
            "kind": kind,
            "filename": filename,
            "size_bytes": size_bytes,
            "metadata": json.loads(meta_json) if meta_json else {},
        }

    # binary
    if not os.path.exists(full):
        return {"error": "vanished"}
    return {
        "kind": "binary",
        "filename": filename,
        "mime": mime,
        "size_bytes": size_bytes,
        "note": "Binary content cannot be read as text. Describe the file "
                "based on its name and mime to the user.",
    }


@function_tool
def read_attachment(attachment_id: str) -> dict:
    """Read the contents of an attachment the user sent in this conversation.

    Use this when the user references a file (PDF, video, log, etc.) you don't
    already see inline. Image attachments are already visible — don't call this
    on them.
    """
    return _read_attachment_impl(
        attachment_id,
        session_id=SESSION_ID_VAR.get(),
        user_id=USER_ID_VAR.get(),
        max_chars=MAX_CHARS_VAR.get(),
    )
