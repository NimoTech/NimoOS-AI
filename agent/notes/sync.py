"""Notes tree ⇄ DB reconciler. Deliberately a FULL-RECONCILE poll (60s):
the notes tree is tiny (hundreds of files), the agent container ships no
inotify library, and full reconcile makes every pass self-healing — there
is no separate 6h fallback because every pass IS the fallback.

Echo suppression: a file whose body sha256 equals notes.content_hash is
our own write — skipped. Reserved OKF files (index.md/log.md) are never
adopted. Identity: frontmatter `id`; a moved file keeps its row."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid

from notes.okf import NOTE_TYPES, parse_note_text, serialize_note_text
from notes.indexer import index_note, deindex_note
from notes import store

_LOG = logging.getLogger("nimoos-agent.notes_sync")

SCAN_SECONDS = 60
_RESERVED = {"index.md", "log.md"}


def _hash(body: str) -> str:
    # store.create_note/update_note hash the raw caller-supplied body
    # (notes/store.py:_hash), but serialize_note_text always guarantees a
    # trailing "\n" on disk. parse_note_text's round-trip therefore hands
    # us that enforced newline back even when the original body lacked
    # one. Strip at most one to stay hash-compatible with store.py's own
    # writes, so our own writes don't bounce back as false "updated".
    if body.endswith("\n"):
        body = body[:-1]
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _walk_md(root: str):
    """Yield (user_id, rel_path, abs_path) for every candidate .md file.
    Layout is <root>/<user_id>/**.md; top-level files are ignored."""
    if not os.path.isdir(root):
        return
    for uid in sorted(os.listdir(root)):
        udir = os.path.join(root, uid)
        if not os.path.isdir(udir) or uid.startswith("."):
            continue
        for dirpath, dirnames, filenames in os.walk(udir):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if not fn.endswith(".md") or fn in _RESERVED \
                        or fn.startswith("."):
                    continue
                ap = os.path.join(dirpath, fn)
                yield uid, os.path.relpath(ap, root), ap


def _meta_note_fields(meta: dict) -> dict:
    t = meta.get("type") if meta.get("type") in NOTE_TYPES else "note"
    status = meta.get("status") if meta.get("status") in (
        "draft", "curated", "archived") else "curated"
    created_by = meta.get("created_by") if meta.get("created_by") in (
        "human", "agent", "pipeline") else "human"
    tags = meta.get("tags") if isinstance(meta.get("tags"), list) else []
    refs = meta.get("source_refs") \
        if isinstance(meta.get("source_refs"), list) else []
    return {"type": t, "status": status, "created_by": created_by,
            "tags": [str(x) for x in tags], "source_refs": refs,
            "title": str(meta.get("title") or ""),
            "description": str(meta.get("description") or "")}


async def scan_once(conn) -> dict:
    stats = {"adopted": 0, "updated": 0, "moved": 0, "deleted": 0}
    root = store.get_notes_root(conn)
    seen_ids: set[str] = set()

    for uid, rel, ap in _walk_md(root):
        try:
            with open(ap, encoding="utf-8") as f:
                meta, body = parse_note_text(f.read())
        except OSError:
            continue
        nid = str(meta.get("id") or "").strip()
        row = conn.execute(
            "SELECT * FROM notes WHERE id=? AND user_id=?",
            (nid, uid)).fetchone() if nid else None

        if row is None:
            # Adoption: hand-created file (or unknown id from elsewhere).
            f = _meta_note_fields(meta)
            now = int(time.time())
            nid = nid or str(uuid.uuid4())
            title = f["title"] or os.path.splitext(
                os.path.basename(rel))[0]
            meta.update({"id": nid, "type": f["type"], "title": title,
                         "status": f["status"],
                         "created_by": f["created_by"]})
            tmp = ap + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(serialize_note_text(meta, body))
            os.replace(tmp, ap)
            conn.execute(
                "INSERT OR IGNORE INTO notes(id, user_id, path, title, "
                "description, type, status, content_hash, source_refs_json, "
                "created_by, revision, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (nid, uid, rel, title, f["description"], f["type"],
                 f["status"], _hash(body),
                 json.dumps(f["source_refs"], ensure_ascii=False),
                 f["created_by"], 1, now, now))
            store._sync_tags(conn, uid, nid, f["tags"])
            conn.commit()
            seen_ids.add(nid)
            stats["adopted"] += 1
            await index_note({"id": nid, "user_id": uid, "type": f["type"],
                              "status": f["status"],
                              "created_by": f["created_by"],
                              "updated_at": now, "title": title}, body)
            continue

        seen_ids.add(nid)
        if row["deleted_at"] is not None:
            # File re-appeared after soft delete → resurrect.
            conn.execute("UPDATE notes SET deleted_at=NULL WHERE id=?",
                         (nid,))
            conn.commit()
        if row["path"] != rel:
            conn.execute("UPDATE notes SET path=? WHERE id=?", (rel, nid))
            conn.commit()
            stats["moved"] += 1
        if row["content_hash"] != _hash(body):
            f = _meta_note_fields(meta)
            now = int(time.time())
            conn.execute(
                "UPDATE notes SET title=?, description=?, type=?, status=?, "
                "content_hash=?, revision=revision+1, updated_at=? "
                "WHERE id=?",
                (f["title"] or row["title"], f["description"], f["type"],
                 f["status"], _hash(body), now, nid))
            store._sync_tags(conn, uid, nid, f["tags"])
            conn.commit()
            stats["updated"] += 1
            await index_note({"id": nid, "user_id": uid, "type": f["type"],
                              "status": f["status"],
                              "created_by": row["created_by"],
                              "updated_at": now,
                              "title": f["title"] or row["title"]}, body)

    # Rows whose file vanished → soft delete + deindex.
    for row in conn.execute(
            "SELECT id, user_id FROM notes WHERE deleted_at IS NULL"):
        if row["id"] in seen_ids:
            continue
        conn.execute("UPDATE notes SET deleted_at=? WHERE id=?",
                     (int(time.time()), row["id"]))
        conn.commit()
        stats["deleted"] += 1
        await deindex_note(row["user_id"], row["id"])
    return stats


async def worker_loop(conn, *, stop_event):
    while not stop_event.is_set():
        try:
            stats = await scan_once(conn)
            if any(stats.values()):
                _LOG.info("notes sync: %s", stats)
        except Exception:
            _LOG.exception("notes sync pass failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=SCAN_SECONDS)
        except asyncio.TimeoutError:
            pass


def start_notes_sync(conn):
    stop_event = asyncio.Event()
    task = asyncio.create_task(worker_loop(conn, stop_event=stop_event))
    return task, stop_event
