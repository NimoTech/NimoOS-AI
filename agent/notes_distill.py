"""Document auto-precipitation worker (spec 2026-07-26): distills documents
under opted-in knowledge roots into draft `summary` notes. Fourth instance of
the memory_extract / recall_index / notes_extract coalescing-job skeleton —
enqueue happens only from the scanner (or an explicit manual request); the
worker NEVER polls the LLM on a timer (wiki summary worker lesson)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import httpx

import memory_lock
from memory_extract import _clean_json_text
from notes import store as notes_store
from notes.indexer import index_note

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3

POLL_SECONDS = 30
# 180s, not the 60s used elsewhere: summarizing a document on a local CPU
# model is far slower than the short extraction prompts, and a too-tight
# timeout turns every slow-but-working run into a wasted retry.
LLM_TIMEOUT = 180
PACE_KNEE = 0.7
PACE_MAX = 60.0

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


def _first_json_object(text: str):
    """Fallback for models that wrap the JSON in prose ('Here is the
    summary: {...}'): decode the first JSON object found in the text,
    ignoring any prose before or after it."""
    idx = text.find("{")
    if idx == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[idx:])
    except ValueError:
        return None
    return obj


def parse_summary(raw):
    """Return a validated summary dict, or None when the payload is not the
    expected JSON shape (caller treats None as a retryable failure)."""
    try:
        cleaned = _clean_json_text(raw)
        obj = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        if not isinstance(raw, str):
            return None
        obj = _first_json_object(_clean_json_text(raw))
        if obj is None:
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


def load_ratio() -> float:
    """1-min loadavg divided by CPU count; 0.0 on any failure. loadavg (not
    CPU%) is deliberate: it counts D-state processes, so a Qdrant write
    bottleneck backpressures us too (same reasoning as Parser's pacing)."""
    try:
        return os.getloadavg()[0] / max(1, os.cpu_count() or 1)
    except (OSError, ValueError):
        return 0.0


def pace_seconds(ratio: float) -> float:
    """Sleep to insert before the next job. Free below the knee, then grows
    linearly up to PACE_MAX."""
    if ratio <= PACE_KNEE:
        return 0.0
    return min(PACE_MAX, (ratio - PACE_KNEE) * 60.0)


def _is_retryable(exc: Exception) -> bool:
    """Parser 4xx means this file will never work (bad path / outside roots /
    gone) — retrying just burns the attempt budget three times over."""
    if isinstance(exc, httpx.HTTPStatusError):
        return not (400 <= exc.response.status_code < 500)
    return True


async def _summarize(llm_call, creds, text: str, *, filename: str) -> str:
    chunks = chunk_text(text)
    if len(chunks) <= 1:
        return await asyncio.wait_for(
            llm_call(creds, build_summary_prompt(text, filename=filename)),
            timeout=LLM_TIMEOUT)
    partials = []
    for i, ch in enumerate(chunks):
        partials.append(await asyncio.wait_for(
            llm_call(creds, build_map_prompt(ch, i, len(chunks),
                                             filename=filename)),
            timeout=LLM_TIMEOUT))
    return await asyncio.wait_for(
        llm_call(creds, build_reduce_prompt(partials, filename=filename)),
        timeout=LLM_TIMEOUT)


async def process_pending_once(conn, *, llm_call, extractor, creds_resolver,
                               note_indexer=index_note, now=None,
                               day=None) -> bool:
    """Claim and run at most one job. Returns False only when there was
    nothing to do — the caller uses that to decide whether to sleep."""
    now = int(time.time()) if now is None else now
    day = time.strftime("%Y%m%d", time.localtime(now)) if day is None else day

    probe = conn.execute(
        "SELECT user_id FROM notes_distill_jobs WHERE status='pending' "
        "ORDER BY enqueued_at ASC LIMIT 1").fetchone()
    if probe is None:
        return False
    quota_ok = notes_store.quota_remaining(conn, probe["user_id"], day=day) > 0
    job = claim_job(conn, quota_ok=quota_ok, now=now)
    if job is None:
        return False

    file_path, user_id = job["file_path"], job["user_id"]
    model = notes_store.get_background_model(conn, user_id)
    if not model:
        # Unconfigured background model = feature off. Drop, don't retry.
        finish_job(conn, file_path)
        return True
    creds = None
    try:
        creds = await creds_resolver(user_id, model)
        if not creds:
            logger.info("notes-distill: dropping %s — credentials unresolved "
                       "for model %r", file_path, model)
            finish_job(conn, file_path)
            return True
        doc = await extractor(file_path, EXTRACT_MAX_CHARS)
        text = (doc or {}).get("markdown") or ""
        if not text.strip():
            logger.info("notes-distill: dropping %s — extract returned no "
                       "text", file_path)
            finish_job(conn, file_path)
            return True
        raw = await _summarize(llm_call, creds, text,
                               filename=os.path.basename(file_path))
        parsed = parse_summary(raw)
        if parsed is None:
            raise ValueError("unparseable summary output")
        await apply_distillation(
            conn, user_id, file_path=file_path, root_id=job["root_id"],
            mtime=job["file_mtime"], parsed=parsed,
            truncated=bool((doc or {}).get("truncated")),
            note_indexer=note_indexer)
        if job["origin"] != "manual":
            notes_store.quota_consume(conn, user_id, day=day)
        finish_job(conn, file_path)
        return True
    except Exception as e:                       # noqa: BLE001 — never die
        msg = str(e) or type(e).__name__
        if creds and creds.get("api_key"):
            msg = msg.replace(creds["api_key"], "***")
        logger.warning("notes-distill failed for %s: %s", file_path, msg)
        attempts = MAX_ATTEMPTS if not _is_retryable(e) else job["attempts"]
        fail_job(conn, file_path, attempts, msg, now)
        return True


async def _default_llm_call(creds: dict, prompt: str) -> str:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(base_url=creds["base_url"], api_key=creds["api_key"],
                         timeout=LLM_TIMEOUT, max_retries=0)
    try:
        resp = await client.chat.completions.create(
            model=creds["model"],
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        return resp.choices[0].message.content or ""
    finally:
        await client.close()


async def _default_extractor(path: str, max_chars: int) -> dict:
    from parser_client import ParserClient
    client = ParserClient()
    try:
        return await client.extract(path, ocr=False, max_chars=max_chars)
    finally:
        await client.aclose()


async def _default_creds(user_id: str, model: str):
    from channels import credentials
    return await credentials.resolve(user_id, model)


async def worker_loop(conn, *, stop_event):
    """One job per tick, serial. Sleeps the full poll interval whenever the
    queue is empty — this worker must never poll the LLM on a timer."""
    while not stop_event.is_set():
        try:
            did_work = await process_pending_once(
                conn, llm_call=_default_llm_call,
                extractor=_default_extractor, creds_resolver=_default_creds)
        except Exception as e:                   # never let the loop die
            logger.exception("notes-distill worker tick error: %s", e)
            did_work = False
        delay = POLL_SECONDS if not did_work else pace_seconds(load_ratio())
        if delay <= 0:
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass


def start_worker(conn):
    """Launch the background worker; returns (task, stop_event)."""
    n = requeue_orphaned(conn)
    if n > 0:
        logger.info("notes distill: requeued %d orphaned running job(s)", n)
    stop_event = asyncio.Event()
    task = asyncio.create_task(worker_loop(conn, stop_event=stop_event))
    return task, stop_event
