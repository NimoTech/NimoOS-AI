"""Fallback reconciler for document distillation (spec §4.1).

This is NOT a redundancy: Wiki's fsnotify watcher degrades a root to
scan_only whenever the kernel refuses more inotify watches
(NimoOS-Wiki/service/scanner/watcher.go, ErrWatchLimit), and three
overlapping watchers on this box make that the normal case rather than an
exception. A full mtime reconcile makes every pass self-healing."""
from __future__ import annotations

import asyncio
import json
import logging
import os

import notes_distill
from notes import store as notes_store

logger = logging.getLogger(__name__)

SCAN_INTERVAL = 60
# Never descend into, nor enqueue, anything dot-prefixed: NimoOS' hidden
# system area, VCS metadata, editor/OS sidecars — and, load-bearingly, Wiki's
# own per-directory `.wiki.md` navigation-map file. That file has a
# distillable `.md` extension, so without this guard opting a root in would
# distill Wiki's nav maps into junk notes (observed live in production).
_SKIP_HIDDEN_PREFIX = "."


def _known_mtimes(conn, user_id: str) -> dict[str, int]:
    """path -> last distilled mtime, from existing summary notes and from
    jobs already queued. Both are needed: a queued-but-not-yet-run file has
    no note yet, and re-enqueueing it every 60s would reset its attempts."""
    known: dict[str, int] = {}
    for r in conn.execute(
            "SELECT source_refs_json FROM notes WHERE user_id=? "
            "AND type='summary' AND deleted_at IS NULL", (str(user_id),)):
        try:
            refs = json.loads(r["source_refs_json"] or "[]")
        except (ValueError, TypeError):
            continue
        if refs and isinstance(refs[0], dict) and refs[0].get("path"):
            known[refs[0]["path"]] = int(refs[0].get("mtime") or 0)
    for r in conn.execute(
            "SELECT file_path, file_mtime FROM notes_distill_jobs "
            "WHERE user_id=?", (str(user_id),)):
        known[r["file_path"]] = max(known.get(r["file_path"], 0),
                                    int(r["file_mtime"]))
    return known


def scan_root(conn, *, user_id: str, root_id: str, root_path: str,
              known: dict[str, int]) -> int:
    """Walk one root, enqueue documents whose mtime is newer than what we
    already distilled. Returns the number enqueued."""
    enqueued = 0
    for dirpath, dirnames, filenames in os.walk(root_path, onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(_SKIP_HIDDEN_PREFIX)]
        for name in filenames:
            if name.startswith(_SKIP_HIDDEN_PREFIX):
                continue
            if not notes_distill.is_distillable(name):
                continue
            full = os.path.join(dirpath, name)
            try:
                mtime = int(os.stat(full).st_mtime)
            except OSError:
                continue
            if mtime <= known.get(full, -1):
                continue
            if notes_distill.enqueue(conn, file_path=full, user_id=user_id,
                                     root_id=root_id, file_mtime=mtime):
                enqueued += 1
    return enqueued


def scan_once(conn, *, user_id: str, roots: list[dict]) -> int:
    """Scan every opted-in, enabled root once."""
    opted_in = set(notes_store.get_distill_roots(conn, user_id))
    if not opted_in:
        return 0
    known = _known_mtimes(conn, user_id)
    total = 0
    for r in roots:
        if str(r.get("id")) not in opted_in or not r.get("enabled"):
            continue
        path = r.get("path") or ""
        if not path:
            continue
        total += scan_root(conn, user_id=user_id, root_id=str(r["id"]),
                           root_path=path, known=known)
    return total


def _opted_in_users(conn) -> list[str]:
    """user_ids that currently have at least one root opted in. A user who
    once opted in and then cleared the list still has a `user_settings` row
    (set_distill_roots stores "[]", it never deletes) — filtering on the
    parsed value, not row presence, is what keeps an emptied-out user from
    triggering a Wiki round-trip every pass forever.

    Also excludes users with an empty background_model: consistent with
    "empty = feature silent" (notes_store.get_background_model docstring) —
    without this gate the scanner would keep walking the filesystem and
    enqueueing jobs that process_pending_once immediately tombstones as
    'skipped' every pass, for a feature the user never turned on."""
    candidates = {r["user_id"] for r in conn.execute(
        "SELECT DISTINCT user_id FROM user_settings WHERE key=?",
        (notes_store.DISTILL_ROOTS_KEY,))}
    return [uid for uid in candidates
            if notes_store.get_distill_roots(conn, uid)
            and notes_store.get_background_model(conn, uid)]


async def _scanner_loop(conn, *, stop_event):
    from wiki_client import WikiClient
    while not stop_event.is_set():
        try:
            users = _opted_in_users(conn)
            if users:
                client = WikiClient()
                try:
                    roots = await client.list_roots()
                finally:
                    await client.aclose()
                for user_id in users:
                    n = scan_once(conn, user_id=user_id, roots=roots)
                    if n:
                        logger.info("notes distill scan: enqueued %d for %s",
                                    n, user_id)
        except Exception as e:                   # never let the loop die
            logger.warning("notes distill scan error: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=SCAN_INTERVAL)
        except asyncio.TimeoutError:
            pass


def start_scanner(conn):
    stop_event = asyncio.Event()
    task = asyncio.create_task(_scanner_loop(conn, stop_event=stop_event))
    return task, stop_event
