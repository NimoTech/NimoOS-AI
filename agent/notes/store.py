"""Notes CRUD: agent.db metadata authority + atomic OKF file writes.

Every write goes file-first (tmp+rename), then the DB row — the scanner
(notes/sync.py) reconciles any crash window between the two. content_hash
is the echo suppressor: the scanner skips files whose body hash matches
the DB, so our own writes never bounce back as external edits."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone

from notes.okf import NOTE_TYPES, serialize_note_text

DEFAULT_NOTES_ROOT = "/DATA/Notes"
_SYSTEM_UID = "__system__"


class RevisionConflict(Exception):
    def __init__(self, current_revision: int):
        super().__init__(f"revision conflict (current={current_revision})")
        self.current_revision = current_revision


def get_notes_root(conn) -> str:
    row = conn.execute(
        "SELECT value FROM user_settings WHERE user_id=? AND key='notes_root'",
        (_SYSTEM_UID,)).fetchone()
    return row["value"] if row and row["value"] else DEFAULT_NOTES_ROOT


def set_notes_root(conn, path: str) -> None:
    conn.execute(
        "INSERT INTO user_settings(user_id, key, value, updated_at) "
        "VALUES (?,?,?,?) ON CONFLICT(user_id, key) DO UPDATE SET "
        "value=excluded.value, updated_at=excluded.updated_at",
        (_SYSTEM_UID, "notes_root", path, int(time.time())))
    conn.commit()


AUTO_EXTRACT_KEY = "notes_auto_extract"


def is_auto_extract_enabled(conn, user_id: str) -> bool:
    """Per-user auto-precipitation toggle; missing row means enabled."""
    row = conn.execute(
        "SELECT value FROM user_settings WHERE user_id=? AND key=?",
        (str(user_id), AUTO_EXTRACT_KEY)).fetchone()
    return row is None or row["value"] != "0"


def set_auto_extract(conn, user_id: str, enabled: bool) -> None:
    conn.execute(
        "INSERT INTO user_settings(user_id, key, value, updated_at) "
        "VALUES (?,?,?,?) ON CONFLICT(user_id, key) DO UPDATE SET "
        "value=excluded.value, updated_at=excluded.updated_at",
        (str(user_id), AUTO_EXTRACT_KEY, "1" if enabled else "0",
         int(time.time())))
    conn.commit()


def note_abs_path(conn, note: dict) -> str:
    return os.path.join(get_notes_root(conn), note["path"])


def _hash(body: str) -> str:
    # Canonicalize to the on-disk form: serialize_note_text always
    # newline-terminates the body, so hash that form here too — sync.py's
    # _hash must stay byte-identical to this one (echo suppression compares
    # the two across store-writes vs. scanner-reads).
    if not body.endswith("\n"):
        body += "\n"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _slug(title: str) -> str:
    s = re.sub(r"[^\w一-鿿-]+", "-", title.strip()).strip("-").lower()
    return (s[:40] or "note")


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


_CONTRACT_COLS = ("id", "user_id", "path", "title", "description", "type",
                  "status", "created_by", "revision", "created_at",
                  "updated_at")


def _row_to_dict(row) -> dict:
    d = {k: row[k] for k in _CONTRACT_COLS}
    d["source_refs"] = json.loads(row["source_refs_json"] or "[]")
    return d


def _atomic_write(abs_path: str, text: str) -> None:
    """tmp+rename, preserving prior owner/mode (or inheriting the parent's
    for brand-new files) so human editors keep write access after our
    rewrites — mirrors fs/staging semantics; chown is best-effort off-root."""
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    try:
        prior = os.stat(abs_path)
    except FileNotFoundError:
        prior = None
    tmp = abs_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, abs_path)
    try:
        if prior is not None:
            os.chown(abs_path, prior.st_uid, prior.st_gid)
            os.chmod(abs_path, prior.st_mode & 0o777)
        else:
            par = os.stat(os.path.dirname(abs_path))
            os.chown(abs_path, par.st_uid, par.st_gid)
            os.chmod(abs_path, par.st_mode & 0o666)
    except PermissionError:
        pass


def _write_note_file(conn, note: dict, body: str) -> None:
    meta = {
        "type": note["type"], "title": note["title"],
        "description": note.get("description", ""),
        "tags": note.get("tags") or [],
        "timestamp": _iso(note["updated_at"]),
        "id": note["id"], "status": note["status"],
        "created_by": note["created_by"],
        "source_refs": note.get("source_refs") or [],
    }
    _atomic_write(note_abs_path(conn, note), serialize_note_text(meta, body))


def _sync_tags(conn, user_id: str, note_id: str, tags: list[str]) -> None:
    """Tags ARE the phase-2 graph seed: type='topic' entities + mentions."""
    now = int(time.time())
    conn.execute("DELETE FROM mentions WHERE note_id=? AND entity_id IN "
                 "(SELECT id FROM entities WHERE type='topic')", (note_id,))
    for tag in dict.fromkeys(t.strip() for t in tags if t.strip()):
        row = conn.execute(
            "SELECT id FROM entities WHERE user_id=? AND type='topic' "
            "AND name=?", (user_id, tag)).fetchone()
        eid = row["id"] if row else str(uuid.uuid4())
        if not row:
            conn.execute(
                "INSERT INTO entities(id, user_id, name, type, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?)",
                (eid, user_id, tag, "topic", now, now))
        conn.execute(
            "INSERT OR IGNORE INTO mentions(entity_id, note_id, chunk_ref) "
            "VALUES (?,?, '')", (eid, note_id))


def _get_row(conn, user_id: str, note_id: str):
    return conn.execute(
        "SELECT * FROM notes WHERE id=? AND user_id=? AND deleted_at IS NULL",
        (note_id, user_id)).fetchone()


def _note_tags(conn, note_id: str) -> list[str]:
    return [r["name"] for r in conn.execute(
        "SELECT e.name FROM mentions m JOIN entities e ON e.id=m.entity_id "
        "WHERE m.note_id=? AND e.type='topic' ORDER BY e.name", (note_id,))]


def create_note(conn, user_id: str, *, title: str, body: str,
                note_type: str = "note", tags: list[str] | None = None,
                source_refs: list[dict] | None = None,
                created_by: str = "human", status: str | None = None,
                description: str = "") -> dict:
    if note_type not in NOTE_TYPES:
        raise ValueError(f"invalid type: {note_type}")
    if status is None:
        status = "draft" if created_by == "pipeline" else "curated"
    now = int(time.time())
    nid = str(uuid.uuid4())
    rel = os.path.join(str(user_id), f"{_slug(title)}-{nid[:8]}.md")
    note = {
        "id": nid, "user_id": str(user_id), "path": rel, "title": title,
        "description": description, "type": note_type, "status": status,
        "tags": list(tags or []), "source_refs": list(source_refs or []),
        "created_by": created_by, "revision": 1,
        "created_at": now, "updated_at": now, "body": body,
    }
    _write_note_file(conn, note, body)
    conn.execute(
        "INSERT INTO notes(id, user_id, path, title, description, type, "
        "status, content_hash, source_refs_json, created_by, revision, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (nid, str(user_id), rel, title, description, note_type, status,
         _hash(body), json.dumps(note["source_refs"], ensure_ascii=False),
         created_by, 1, now, now))
    _sync_tags(conn, str(user_id), nid, note["tags"])
    sync_links(conn, nid, body)
    conn.commit()
    return note


def update_note(conn, user_id: str, note_id: str, *, expected_revision: int,
                title: str | None = None, body: str | None = None,
                note_type: str | None = None, tags: list[str] | None = None,
                status: str | None = None,
                description: str | None = None,
                source_refs: list[dict] | None = None) -> dict:
    row = _get_row(conn, user_id, note_id)
    if row is None:
        raise KeyError(note_id)
    if row["revision"] != expected_revision:
        raise RevisionConflict(row["revision"])
    note = _row_to_dict(row)
    if body is None:
        cur = get_note(conn, user_id, note_id)
        body = cur["body"] if cur else ""
    if title is not None:
        note["title"] = title
    if note_type is not None:
        if note_type not in NOTE_TYPES:
            raise ValueError(f"invalid type: {note_type}")
        note["type"] = note_type
    if status is not None:
        note["status"] = status
    if description is not None:
        note["description"] = description
    if source_refs is not None:
        note["source_refs"] = list(source_refs)
    note["tags"] = list(tags) if tags is not None \
        else _note_tags(conn, note_id)
    note["revision"] += 1
    note["updated_at"] = int(time.time())
    note["body"] = body
    # path never changes on update; renames are scanner territory (sync.py)
    _write_note_file(conn, note, body)
    conn.execute(
        "UPDATE notes SET title=?, description=?, type=?, status=?, "
        "content_hash=?, source_refs_json=?, revision=?, updated_at=? "
        "WHERE id=? AND user_id=?",
        (note["title"], note["description"], note["type"], note["status"],
         _hash(body), json.dumps(note["source_refs"], ensure_ascii=False),
         note["revision"], note["updated_at"], note_id, str(user_id)))
    _sync_tags(conn, str(user_id), note_id, note["tags"])
    sync_links(conn, note_id, body)
    conn.commit()
    return note


def get_note(conn, user_id: str, note_id: str) -> dict | None:
    row = _get_row(conn, user_id, note_id)
    if row is None:
        return None
    note = _row_to_dict(row)
    note["tags"] = _note_tags(conn, note_id)
    try:
        with open(note_abs_path(conn, note), encoding="utf-8") as f:
            from notes.okf import parse_note_text
            _, note["body"] = parse_note_text(f.read())
    except OSError:
        note["body"] = ""
        note["file_missing"] = True
    return note


def list_notes(conn, user_id: str, *, note_type: str | None = None,
               status: str | None = None, limit: int = 50) -> list[dict]:
    q = ("SELECT * FROM notes WHERE user_id=? AND deleted_at IS NULL")
    args: list = [str(user_id)]
    if note_type:
        q += " AND type=?"
        args.append(note_type)
    if status:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY updated_at DESC LIMIT ?"
    args.append(int(limit))
    out = []
    for row in conn.execute(q, args):
        d = _row_to_dict(row)
        d["tags"] = _note_tags(conn, d["id"])
        out.append(d)
    return out


def soft_delete_note(conn, user_id: str, note_id: str) -> bool:
    row = _get_row(conn, user_id, note_id)
    if row is None:
        return False
    try:
        os.remove(note_abs_path(conn, _row_to_dict(row)))
    except OSError:
        pass
    conn.execute("UPDATE notes SET deleted_at=? WHERE id=? AND user_id=?",
                 (int(time.time()), note_id, str(user_id)))
    conn.execute("DELETE FROM mentions WHERE note_id=?", (note_id,))
    conn.commit()
    return True


def sync_links(conn, note_id: str, body: str) -> None:
    from notes.links import extract_links
    conn.execute("DELETE FROM note_links WHERE src_note_id=?", (note_id,))
    for l in extract_links(body):
        conn.execute(
            "INSERT OR IGNORE INTO note_links(src_note_id, dst_kind, "
            "dst_ref, anchor_text) VALUES (?,?,?,?)",
            (note_id, l["dst_kind"], l["dst_ref"], l["anchor_text"]))


def get_backlinks(conn, user_id: str, note_id: str) -> list[dict]:
    row = _get_row(conn, user_id, note_id)
    if row is None:
        return []
    rel = row["path"]
    base = os.path.splitext(os.path.basename(rel))[0]
    refs = (rel, "/" + rel, base, base + ".md", row["title"])
    q = ("SELECT DISTINCT n.id, n.title, n.type, n.status, n.updated_at "
         "FROM note_links l JOIN notes n ON n.id = l.src_note_id "
         "WHERE l.dst_kind='note' AND n.user_id=? AND n.deleted_at IS NULL "
         "AND n.id != ? AND l.dst_ref IN (?,?,?,?,?) "
         "ORDER BY n.updated_at DESC")
    return [dict(r) for r in conn.execute(q, (str(user_id), note_id, *refs))]


DISTILL_ROOTS_KEY = "notes_distill_roots"
DISTILL_CAP_KEY = "notes_distill_daily_cap"
DISTILL_QUOTA_KEY = "notes_distill_quota"
BACKGROUND_MODEL_KEY = "background_model"
DEFAULT_DAILY_CAP = 50


def _get_setting(conn, user_id: str, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM user_settings WHERE user_id=? AND key=?",
        (str(user_id), key)).fetchone()
    return row["value"] if row else None


def _set_setting(conn, user_id: str, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO user_settings(user_id, key, value, updated_at) "
        "VALUES (?,?,?,?) ON CONFLICT(user_id, key) DO UPDATE SET "
        "value=excluded.value, updated_at=excluded.updated_at",
        (str(user_id), key, value, int(time.time())))
    conn.commit()


def get_distill_roots(conn, user_id: str) -> list[str]:
    """Wiki root ids opted into auto-distillation. Empty = feature silent."""
    raw = _get_setting(conn, user_id, DISTILL_ROOTS_KEY)
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [str(x) for x in val] if isinstance(val, list) else []


def set_distill_roots(conn, user_id: str, root_ids) -> None:
    _set_setting(conn, user_id, DISTILL_ROOTS_KEY,
                 json.dumps([str(r) for r in root_ids], ensure_ascii=False))


def get_daily_cap(conn, user_id: str) -> int:
    try:
        cap = int(_get_setting(conn, user_id, DISTILL_CAP_KEY))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_CAP
    return cap if cap >= 0 else DEFAULT_DAILY_CAP


def set_daily_cap(conn, user_id: str, cap: int) -> None:
    _set_setting(conn, user_id, DISTILL_CAP_KEY, str(int(cap)))


def get_background_model(conn, user_id: str) -> str:
    """Model key for background tasks: 'cloud:<pid>:<model>' or a bare
    Ollama model name. Empty means unconfigured — feature stays off."""
    return _get_setting(conn, user_id, BACKGROUND_MODEL_KEY) or ""


def set_background_model(conn, user_id: str, model: str) -> None:
    _set_setting(conn, user_id, BACKGROUND_MODEL_KEY, str(model))


def _quota_used(conn, user_id: str, day: str) -> int:
    raw = _get_setting(conn, user_id, DISTILL_QUOTA_KEY)
    if not raw:
        return 0
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return 0
    if not isinstance(obj, dict) or obj.get("day") != day:
        return 0
    try:
        return max(0, int(obj.get("used", 0)))
    except (TypeError, ValueError):
        return 0


def quota_remaining(conn, user_id: str, *, day: str) -> int:
    return max(0, get_daily_cap(conn, user_id) - _quota_used(conn, user_id, day))


def quota_consume(conn, user_id: str, *, day: str) -> None:
    """Bump today's counter. A stale day key resets to 1 (no cron needed)."""
    _set_setting(conn, user_id, DISTILL_QUOTA_KEY,
                 json.dumps({"day": day,
                             "used": _quota_used(conn, user_id, day) + 1}))
