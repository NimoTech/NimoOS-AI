"""Document auto-precipitation worker (spec 2026-07-26): distills documents
under opted-in knowledge roots into draft `summary` notes. Fourth instance of
the memory_extract / recall_index / notes_extract coalescing-job skeleton —
enqueue happens only from the scanner (or an explicit manual request); the
worker NEVER polls the LLM on a timer (wiki summary worker lesson)."""
from __future__ import annotations

import json
import logging
import os
import time

import memory_lock
from memory_extract import _clean_json_text
from notes import store as notes_store
from notes.indexer import index_note

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3

# Document subset of Parser's TEXT_EXT_ALLOWLIST. Source-code extensions are
# deliberately excluded: Parser does not error on unknown extensions (it reads
# them as replacement-char text), so a code repo under a knowledge root would
# otherwise flood the notes library with garbage summaries.
DISTILL_EXTS = frozenset({
    ".md", ".txt", ".rst",
    ".pdf",
    ".docx", ".doc", ".wps",
    ".pptx", ".ppt",
    ".xlsx", ".xls",
    ".odt",
    ".html", ".htm",
})


def is_distillable(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in DISTILL_EXTS


CHUNK_CHARS = 12000
MAX_CHUNKS = 8
EXTRACT_MAX_CHARS = CHUNK_CHARS * MAX_CHUNKS

_JSON_CONTRACT = (
    'Output STRICT JSON and nothing else:\n'
    '{"title":"<short title>","description":"<one-line summary>",'
    '"body":"<markdown summary>","tags":["tag"]}\n'
    "Write in the same language as the document. Use standard markdown "
    "links, never [[wikilinks]]."
)


def chunk_text(text: str, size: int = CHUNK_CHARS,
               max_chunks: int = MAX_CHUNKS) -> list[str]:
    """Fixed-width split with a hard chunk cap. The cap is the cost ceiling:
    without it a 500-page scan would cost dozens of LLM calls."""
    if not text:
        return []
    return [text[i:i + size]
            for i in range(0, len(text), size)][:max_chunks]


def build_summary_prompt(text: str, *, filename: str) -> str:
    return (
        f"Summarize the document '{filename}' for a personal knowledge base. "
        "Capture what it is about, its key conclusions, decisions, figures "
        "and obligations. Skip boilerplate.\n"
        f"{_JSON_CONTRACT}\n\n## Document\n{text}"
    )


def build_map_prompt(chunk: str, idx: int, total: int, *, filename: str) -> str:
    return (
        f"This is part {idx + 1} of {total} of the document '{filename}'. "
        "Write a dense factual digest of THIS PART only, as plain markdown "
        "(no JSON). Preserve names, dates, figures and obligations.\n\n"
        f"## Part\n{chunk}"
    )


def build_reduce_prompt(partials: list[str], *, filename: str) -> str:
    joined = "\n\n---\n\n".join(
        f"### Part {i + 1}\n{p}" for i, p in enumerate(partials))
    return (
        f"Below are sequential digests of the document '{filename}'. "
        "Merge them into one coherent summary for a personal knowledge base. "
        "Do not mention that the document was processed in parts.\n"
        f"{_JSON_CONTRACT}\n\n## Digests\n{joined}"
    )


def parse_summary(raw):
    """Return a validated summary dict, or None when the payload is not the
    expected JSON shape (caller treats None as a retryable failure)."""
    try:
        obj = json.loads(_clean_json_text(raw))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    title, body = obj.get("title"), obj.get("body")
    if not (isinstance(title, str) and title.strip()
            and isinstance(body, str) and body.strip()):
        return None
    desc = obj.get("description")
    tags_raw = obj.get("tags")
    tags = [t.strip() for t in tags_raw
            if isinstance(t, str) and t.strip()] \
        if isinstance(tags_raw, list) else []
    return {"title": title.strip(),
            "description": desc.strip() if isinstance(desc, str) else "",
            "body": body.strip(), "tags": tags}


def enqueue(conn, *, file_path: str, user_id: str, root_id: str,
            file_mtime: int, origin: str = "auto", now=None) -> bool:
    """Coalescing UPSERT keyed on file_path. A manual request outranks a
    later auto one — never demote origin back to 'auto'."""
    if not is_distillable(file_path):
        return False
    now = int(time.time()) if now is None else now
    conn.execute(
        """INSERT INTO notes_distill_jobs
             (file_path, user_id, root_id, file_mtime, status, attempts,
              origin, enqueued_at, updated_at)
           VALUES (?,?,?,?,'pending',0,?,?,?)
           ON CONFLICT(file_path) DO UPDATE SET
             status='pending', attempts=0, last_error=NULL,
             user_id=excluded.user_id, root_id=excluded.root_id,
             file_mtime=excluded.file_mtime,
             origin=CASE WHEN notes_distill_jobs.origin='manual'
                         THEN 'manual' ELSE excluded.origin END,
             enqueued_at=excluded.enqueued_at,
             updated_at=excluded.updated_at""",
        (file_path, str(user_id), str(root_id), int(file_mtime), origin,
         now, now))
    conn.commit()
    return True


def claim_job(conn, *, quota_ok: bool, now=None):
    """Claim one pending job, manual first. Auto jobs are invisible once the
    daily quota is spent; manual jobs always jump the queue."""
    now = int(time.time()) if now is None else now
    sql = ("SELECT * FROM notes_distill_jobs WHERE status='pending' "
           "{extra} ORDER BY CASE origin WHEN 'manual' THEN 0 ELSE 1 END, "
           "enqueued_at ASC LIMIT 1")
    row = conn.execute(sql.format(
        extra="" if quota_ok else "AND origin='manual'")).fetchone()
    if row is None:
        return None
    attempts = row["attempts"] + 1
    conn.execute(
        "UPDATE notes_distill_jobs SET status='running', attempts=?, "
        "updated_at=? WHERE file_path=?", (attempts, now, row["file_path"]))
    conn.commit()
    return conn.execute("SELECT * FROM notes_distill_jobs WHERE file_path=?",
                        (row["file_path"],)).fetchone()


def finish_job(conn, file_path: str) -> None:
    conn.execute("DELETE FROM notes_distill_jobs "
                 "WHERE file_path=? AND status='running'", (file_path,))
    conn.commit()


def fail_job(conn, file_path: str, attempts: int, err, now: int) -> None:
    if attempts >= MAX_ATTEMPTS:
        conn.execute("DELETE FROM notes_distill_jobs "
                     "WHERE file_path=? AND status='running'", (file_path,))
    else:
        conn.execute(
            "UPDATE notes_distill_jobs SET status='pending', last_error=?, "
            "updated_at=? WHERE file_path=? AND status='running'",
            (str(err)[:500], now, file_path))
    conn.commit()


def requeue_orphaned(conn) -> int:
    """A row still 'running' at startup was claimed by a dead process — this
    worker is the table's only consumer, so flip it back to pending."""
    cur = conn.execute(
        "UPDATE notes_distill_jobs SET status='pending' WHERE status='running'")
    conn.commit()
    return cur.rowcount


def find_summary_note(conn, user_id: str, file_path: str) -> dict | None:
    """Locate this document's existing summary note. LIKE is only a
    prefilter — identity is an exact match on source_refs[0].path, so a
    path that is a prefix of another never collides."""
    rows = conn.execute(
        "SELECT id, revision, status, source_refs_json FROM notes "
        "WHERE user_id=? AND type='summary' AND deleted_at IS NULL "
        "AND source_refs_json LIKE ?",
        (str(user_id), f"%{file_path}%")).fetchall()
    for r in rows:
        try:
            refs = json.loads(r["source_refs_json"] or "[]")
        except (ValueError, TypeError):
            continue
        if refs and isinstance(refs[0], dict) and refs[0].get("path") == file_path:
            return {"id": r["id"], "revision": r["revision"],
                    "status": r["status"], "source_refs": refs}
    return None


async def apply_distillation(conn, user_id: str, *, file_path: str,
                             root_id: str, mtime: int, parsed: dict,
                             truncated: bool, note_indexer=index_note):
    """Create or refresh this document's summary note. DB writes run under
    the per-user lock; Qdrant indexing runs outside it (same discipline as
    memory_extract / notes_extract)."""
    refs = [{"path": file_path, "root_id": str(root_id), "mtime": int(mtime),
             "truncated": bool(truncated)}]
    async with memory_lock.get_user_lock(str(user_id)):
        existing = find_summary_note(conn, user_id, file_path)
        if existing and existing["status"] == "curated":
            # Never clobber what a human has confirmed — flag it and let the
            # user decide whether to re-distill.
            stale_refs = list(existing["source_refs"])
            stale_refs[0] = {**stale_refs[0], "stale": True}
            note = notes_store.update_note(
                conn, str(user_id), existing["id"],
                expected_revision=existing["revision"],
                source_refs=stale_refs)
            return note
        if existing:
            note = notes_store.update_note(
                conn, str(user_id), existing["id"],
                expected_revision=existing["revision"],
                title=parsed["title"], body=parsed["body"],
                description=parsed["description"], tags=parsed["tags"],
                source_refs=refs)
        else:
            note = notes_store.create_note(
                conn, str(user_id), title=parsed["title"], body=parsed["body"],
                note_type="summary", tags=parsed["tags"], source_refs=refs,
                created_by="pipeline", description=parsed["description"])
    ok = await note_indexer(note, note["body"])
    if not ok:
        # Pending-index sentinel (mirrors notes_extract): keep the note, let
        # the sync scanner retry indexing next pass.
        conn.execute("UPDATE notes SET content_hash='' WHERE id=? AND user_id=?",
                     (note["id"], str(user_id)))
        conn.commit()
    return note
