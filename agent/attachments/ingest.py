"""Register an already-on-disk file (e.g. a channel download saved under /DATA)
as a session attachment WITHOUT copying: create a symlink under the session
attachments dir pointing at the real file, and insert a normal attachments row
(rel_path = the symlink basename). Existing read paths join
data_root/sessions/<sid>/attachments/<rel_path> and open(), transparently
following the symlink; GC removing the symlink leaves the real file intact."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid

from .kind import classify
from .paths import sanitize_filename


def ingest_external(conn: sqlite3.Connection, data_root: str, session_id: str,
                    *, real_path: str, filename: str) -> str:
    if not os.path.isfile(real_path):
        raise FileNotFoundError(real_path)
    mime, kind = classify(real_path, filename)
    size = os.path.getsize(real_path)
    aid = "att_" + uuid.uuid4().hex[:12]
    rel_name = f"{aid}__{sanitize_filename(filename)}"
    link_dir = os.path.join(data_root, "sessions", session_id, "attachments")
    os.makedirs(link_dir, exist_ok=True)
    link_path = os.path.join(link_dir, rel_name)
    os.symlink(os.path.abspath(real_path), link_path)
    now = int(time.time())
    conn.execute(
        "INSERT INTO attachments "
        "(id,session_id,message_id,filename,mime,kind,size_bytes,rel_path,"
        " meta_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (aid, session_id, None, filename, mime, kind, size, rel_name,
         json.dumps({"external": True}), now),
    )
    conn.commit()
    return aid
