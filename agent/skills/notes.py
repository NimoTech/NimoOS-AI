"""Knowledge-notes tools. Identity via USER_ID_VAR (never an LLM param).
Writes go through the confirmation card; user approval IS the human
sign-off, so confirmed agent notes land as status='curated' (spec §7.1 —
draft is reserved for the M3 auto-extraction pipeline)."""
from __future__ import annotations

import json
from contextvars import ContextVar

from agents import function_tool

import db as db_module
from notes import store
from notes.indexer import index_note
from notes.okf import NOTE_TYPES

USER_ID_VAR: ContextVar[str] = ContextVar("notes_user_id", default="")
SESSION_ID_VAR: ContextVar[str] = ContextVar("notes_session_id", default="")
CONFIRM_MGR_VAR: ContextVar = ContextVar("notes_confirm_mgr", default=None)
EVENT_QUEUE_VAR: ContextVar = ContextVar("notes_event_queue", default=None)

TEXT_PREVIEW_CAP = 200


def _preview(text: str) -> str:
    if len(text) <= TEXT_PREVIEW_CAP:
        return text
    return f"{text[:TEXT_PREVIEW_CAP]}…(+{len(text) - TEXT_PREVIEW_CAP} more chars)"


async def _request_confirm(action: str, description: str, command: str) -> bool:
    mgr = CONFIRM_MGR_VAR.get()
    sink = EVENT_QUEUE_VAR.get()
    session_id = SESSION_ID_VAR.get()
    if mgr is None or sink is None or not session_id:
        return False   # misconfigured runtime — refuse write, never bypass
    confirm_id = mgr.register(session_id, action, description, command)
    await sink.put({
        "type": "confirmation_required",
        "confirm_id": confirm_id,
        "action": action,
        "description": description,
        "command": command,
    })
    return await mgr.wait(confirm_id)


def _uid_or_err() -> tuple[str, str | None]:
    uid = USER_ID_VAR.get()
    if not uid:
        return "", json.dumps({"error": "no user context"}, ensure_ascii=False)
    return uid, None


def _public(note: dict) -> dict:
    return {k: note.get(k) for k in
            ("id", "title", "description", "type", "status", "tags",
             "source_refs", "created_by", "revision", "updated_at", "path")}


async def _write_note_impl(title: str, content: str, note_type: str,
                           tags: list[str], source_files: list[str]) -> str:
    uid, err = _uid_or_err()
    if err:
        return err
    if note_type not in NOTE_TYPES:
        return json.dumps(
            {"error": f"invalid type: {note_type}; use one of {NOTE_TYPES}"},
            ensure_ascii=False)
    description = f"Create knowledge note: {title}"
    command = f"write_note type={note_type} tags={tags}\n→ {_preview(content)}"
    if CONFIRM_MGR_VAR.get() is None or EVENT_QUEUE_VAR.get() is None \
            or not SESSION_ID_VAR.get():
        return json.dumps({"error": "confirm channel unavailable"},
                          ensure_ascii=False)
    if not await _request_confirm("notes_write", description, command):
        return json.dumps({"error": "user declined"}, ensure_ascii=False)
    conn = db_module.get_connection()
    refs = [{"path": p} for p in (source_files or [])]
    note = store.create_note(conn, uid, title=title, body=content,
                             note_type=note_type, tags=list(tags or []),
                             source_refs=refs, created_by="agent",
                             status="curated")
    ok = await index_note(note, content)
    if not ok:
        # Pending-index sentinel (mirrors notes/sync.py): the note is saved
        # regardless, but content_hash='' forces the sync scanner's
        # hash-mismatch branch to retry indexing next pass instead of
        # treating the (already-hashed) body as up to date.
        conn.execute("UPDATE notes SET content_hash='' WHERE id=? AND user_id=?",
                     (note["id"], uid))
        conn.commit()
    return json.dumps({"ok": True, "id": note["id"],
                       "status": note["status"]}, ensure_ascii=False)


async def _update_note_impl(note_id: str, expected_revision: int,
                            content: str = "", title: str = "",
                            status: str = "", tags: list[str] | None = None
                            ) -> str:
    uid, err = _uid_or_err()
    if err:
        return err
    cur = store.get_note(db_module.get_connection(), uid, note_id)
    if cur is None:
        return json.dumps({"error": "note not found"}, ensure_ascii=False)
    description = f"Update knowledge note: {cur['title']}"
    command = (f"update_note id={note_id} rev={expected_revision}\n"
               f"→ {_preview(content or '(meta only)')}")
    if CONFIRM_MGR_VAR.get() is None or EVENT_QUEUE_VAR.get() is None \
            or not SESSION_ID_VAR.get():
        return json.dumps({"error": "confirm channel unavailable"},
                          ensure_ascii=False)
    if not await _request_confirm("notes_update", description, command):
        return json.dumps({"error": "user declined"}, ensure_ascii=False)
    conn = db_module.get_connection()
    try:
        note = store.update_note(
            conn, uid, note_id, expected_revision=expected_revision,
            title=title or None, body=content or None,
            status=status or None, tags=tags)
    except store.RevisionConflict as e:
        return json.dumps({"error": "revision conflict",
                           "current_revision": e.current_revision,
                           "hint": "re-read with read_note and retry"},
                          ensure_ascii=False)
    ok = await index_note(note, note["body"])
    if not ok:
        # Pending-index sentinel (mirrors notes/sync.py): keep the update,
        # but force the sync scanner to retry indexing next pass.
        conn.execute("UPDATE notes SET content_hash='' WHERE id=? AND user_id=?",
                     (note["id"], uid))
        conn.commit()
    return json.dumps({"ok": True, "revision": note["revision"]},
                      ensure_ascii=False)


async def _read_note_impl(note_id: str) -> str:
    uid, err = _uid_or_err()
    if err:
        return err
    note = store.get_note(db_module.get_connection(), uid, note_id)
    if note is None:
        return json.dumps({"error": "note not found"}, ensure_ascii=False)
    out = _public(note)
    out["body"] = note.get("body", "")
    return json.dumps(out, ensure_ascii=False)


async def _list_notes_impl(note_type: str, status: str, limit: int) -> str:
    uid, err = _uid_or_err()
    if err:
        return err
    rows = store.list_notes(db_module.get_connection(), uid,
                            note_type=note_type or None,
                            status=status or None,
                            limit=max(1, min(limit, 100)))
    return json.dumps({"notes": [_public(n) for n in rows]},
                      ensure_ascii=False)


@function_tool
async def write_note(title: str, content: str, note_type: str = "note",
                     tags: list[str] | None = None,
                     source_files: list[str] | None = None) -> str:
    """Save a knowledge note to the user's knowledge base. Pops a
    confirmation card; on approval the note is stored as curated knowledge,
    written as a Markdown file the user can also open/edit directly.

    Use when the user says "write this down/save it to my knowledge base" or asks to keep a conclusion,
    summary, or decision for the long term.

    Args:
        title: Short human-readable title.
        content: Markdown body. Use standard markdown links, not [[wikilinks]].
        note_type: One of note|summary|insight|digest.
        tags: Topic tags (become browsable topics).
        source_files: Absolute paths of files this note is based on.
    """
    return await _write_note_impl(title, content, note_type,
                                  list(tags or []), list(source_files or []))


@function_tool
async def update_note(note_id: str, expected_revision: int,
                      content: str = "", title: str = "",
                      status: str = "", tags: list[str] | None = None) -> str:
    """Update an existing knowledge note (confirmation required). Pass the
    revision you read via read_note/list_notes; on 'revision conflict'
    re-read and retry. Empty string fields are left unchanged.

    Args:
        note_id: The note id.
        expected_revision: Revision from your last read (optimistic lock).
        content: New full Markdown body ('' = keep).
        title: New title ('' = keep).
        status: draft|curated|archived ('' = keep).
        tags: Replacement tag list (None = keep).
    """
    return await _update_note_impl(note_id, expected_revision, content,
                                   title, status, tags)


@function_tool
async def read_note(note_id: str) -> str:
    """Read one knowledge note (metadata + full Markdown body)."""
    return await _read_note_impl(note_id)


@function_tool
async def list_notes(note_type: str = "", status: str = "",
                     limit: int = 20) -> str:
    """List the user's knowledge notes, newest first.

    Args:
        note_type: Filter by note|summary|insight|digest ('' = all).
        status: Filter by draft|curated|archived ('' = all).
        limit: Max results (1..100).
    """
    return await _list_notes_impl(note_type, status, limit)


NOTES_TOOLS = [write_note, update_note, read_note, list_notes]
