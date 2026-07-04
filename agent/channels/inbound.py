"""Save inbound channel attachments to the durable download dir and register
them as session attachments (via symlink). Enforces per-file / per-message
count / per-message total-size caps. Temp files are always removed."""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3

from attachments.ingest import ingest_external
from attachments.paths import sanitize_filename

_LOG = logging.getLogger("nimoos-agent.channels")

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_COUNT = 10


def _unique_dest(ddir: str, filename: str) -> str:
    base = sanitize_filename(filename)
    dest = os.path.join(ddir, base)
    if not os.path.exists(dest):
        return dest
    stem, ext = os.path.splitext(base)
    i = 1
    while True:
        cand = os.path.join(ddir, f"{stem} ({i}){ext}")
        if not os.path.exists(cand):
            return cand
        i += 1


def save_and_ingest(conn: sqlite3.Connection, data_root: str, session_id: str,
                    download_dir: str, attachments, *,
                    max_file: int = MAX_FILE_BYTES,
                    max_total: int = MAX_TOTAL_BYTES,
                    max_count: int = MAX_COUNT):
    ids: list[str] = []
    skipped: list[str] = []
    running_total = 0
    try:
        os.makedirs(download_dir, exist_ok=True)
        for idx, att in enumerate(attachments):
            if idx >= max_count:
                skipped.append(att.filename)
                continue
            real_size = os.path.getsize(att.tmp_path)   # never trust caller-supplied att.size
            if real_size > max_file or running_total + real_size > max_total:
                skipped.append(att.filename)
                continue
            try:
                dest = _unique_dest(download_dir, att.filename)
                shutil.move(att.tmp_path, dest)      # tmp -> /DATA (real bytes)
                running_total += real_size
                aid = ingest_external(conn, data_root, session_id,
                                      real_path=dest, filename=os.path.basename(dest))
                ids.append(aid)
            except Exception:
                _LOG.warning("failed to save/ingest attachment %r", att.filename,
                            exc_info=True)
                skipped.append(att.filename)
                continue
    finally:
        for att in attachments:                  # any leftover tmp (skipped / error)
            try:
                if att.tmp_path and os.path.exists(att.tmp_path):
                    os.remove(att.tmp_path)
            except OSError:
                pass
    return ids, skipped
