"""Notes tree ⇄ DB reconciler. Deliberately a FULL-RECONCILE poll (60s):
the notes tree is tiny (hundreds of files), the agent container ships no
inotify library, and full reconcile makes every pass self-healing — there
is no separate 6h fallback because every pass IS the fallback.

Echo suppression: a file whose body sha256 equals notes.content_hash is
our own write — skipped. Reserved OKF files (index.md/log.md) are never
adopted. Identity: frontmatter `id`; a moved file keeps its row.

Self-healing invariants:
- content_hash='' is a pending-index sentinel, never a real hash. Any row
  with an empty hash is picked up by the mismatch branch next pass and
  retried — used both for resurrect-after-soft-delete (file content is
  unchanged so the real hash would already "match" and skip reindexing)
  and for a failed index_note() call (so the write isn't silently lost).
  A retry may bump `revision` again even though content didn't change
  again — acceptable, it's just a signal that indexing was attempted.
- deindex_note() is called BEFORE a row is marked deleted_at; if it
  returns False the row is left alive (still absent on disk) so the next
  pass retries the deindex instead of leaving orphaned vectors forever.
- Per-file processing is fully isolated: an unreadable/corrupt file
  (e.g. non-UTF8 bytes) is logged and skipped without aborting the walk
  or the vanished-file soft-delete pass. Its path is remembered for this
  pass so the vanished-file pass never treats "unreadable" as "deleted"."""
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
    # Must stay byte-identical to notes/store.py:_hash — both canonicalize
    # to the on-disk form (serialize_note_text always newline-terminates)
    # before hashing, so store-side writes and scanner-side reads of the
    # same file always agree, regardless of whether the original caller
    # body already ended in "\n".
    if not body.endswith("\n"):
        body += "\n"
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
    failed_paths: set[str] = set()

    for uid, rel, ap in _walk_md(root):
        try:
            with open(ap, encoding="utf-8") as f:
                meta, body = parse_note_text(f.read())
            nid = str(meta.get("id") or "").strip()
            row = conn.execute(
                "SELECT * FROM notes WHERE id=? AND user_id=?",
                (nid, uid)).fetchone() if nid else None

            if row is None:
                if nid:
                    foreign = conn.execute(
                        "SELECT user_id FROM notes WHERE id=?", (nid,)).fetchone()
                    if foreign is not None and foreign["user_id"] != uid:
                        _LOG.warning("notes sync: %s carries id owned by another "
                                     "user — skipped", rel)
                        continue
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
                    "description, type, status, content_hash, "
                    "source_refs_json, created_by, revision, created_at, "
                    "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (nid, uid, rel, title, f["description"], f["type"],
                     f["status"], _hash(body),
                     json.dumps(f["source_refs"], ensure_ascii=False),
                     f["created_by"], 1, now, now))
                store._sync_tags(conn, uid, nid, f["tags"])
                store.sync_links(conn, nid, body)
                conn.commit()
                seen_ids.add(nid)
                stats["adopted"] += 1
                ok = await index_note(
                    {"id": nid, "user_id": uid, "type": f["type"],
                     "status": f["status"], "created_by": f["created_by"],
                     "updated_at": now, "title": title}, body)
                if not ok:
                    # Pending-index sentinel: next pass's hash-mismatch
                    # branch retries the index instead of losing the write.
                    conn.execute(
                        "UPDATE notes SET content_hash='' WHERE id=?",
                        (nid,))
                    conn.commit()
                continue

            seen_ids.add(nid)
            db_content_hash = row["content_hash"]
            if row["deleted_at"] is not None:
                # File re-appeared after soft delete → resurrect. Content
                # may be byte-identical to what was last indexed (it was
                # deindexed on delete), so force a reindex via the
                # pending-index sentinel rather than relying on hash
                # mismatch, which would otherwise never fire.
                conn.execute(
                    "UPDATE notes SET deleted_at=NULL, content_hash='' "
                    "WHERE id=?", (nid,))
                conn.commit()
                db_content_hash = ""
            if row["path"] != rel:
                conn.execute("UPDATE notes SET path=? WHERE id=?",
                             (rel, nid))
                conn.commit()
                stats["moved"] += 1
            if db_content_hash != _hash(body):
                f = _meta_note_fields(meta)
                now = int(time.time())
                ok = await index_note(
                    {"id": nid, "user_id": uid, "type": f["type"],
                     "status": f["status"],
                     "created_by": row["created_by"], "updated_at": now,
                     "title": f["title"] or row["title"]}, body)
                new_hash = _hash(body) if ok else ""
                conn.execute(
                    "UPDATE notes SET title=?, description=?, type=?, "
                    "status=?, content_hash=?, revision=revision+1, "
                    "updated_at=? WHERE id=?",
                    (f["title"] or row["title"], f["description"],
                     f["type"], f["status"], new_hash, now, nid))
                store._sync_tags(conn, uid, nid, f["tags"])
                store.sync_links(conn, nid, body)
                conn.commit()
                stats["updated"] += 1
        except Exception:
            _LOG.warning("notes sync: failed to process %s", ap,
                        exc_info=True)
            failed_paths.add(rel)
            continue

    # Rows whose file vanished → soft delete + deindex. An unreadable file
    # (in failed_paths) is NOT a deleted file — skip it so it isn't wrongly
    # soft-deleted while it's merely unparsable this pass.
    for row in conn.execute(
            "SELECT id, user_id, path FROM notes WHERE deleted_at IS NULL"):
        if row["id"] in seen_ids or row["path"] in failed_paths:
            continue
        ok = await deindex_note(row["user_id"], row["id"])
        if not ok:
            _LOG.warning("notes sync: deindex failed for %s, will retry",
                        row["id"])
            continue
        conn.execute("UPDATE notes SET deleted_at=? WHERE id=?",
                     (int(time.time()), row["id"]))
        conn.commit()
        stats["deleted"] += 1
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
