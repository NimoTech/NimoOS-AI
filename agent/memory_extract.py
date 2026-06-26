"""Session-idle memory auto-extraction (P3). A startup asyncio worker, after a
session goes quiet, asks the conversation's own model to distill durable user
facts and applies ADD/UPDATE/NOOP to the profile store. Fully async — never on
the chat main path; bounded by hard timeout + attempt cap + sequential single-flight.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import memory_store
from memory_lock import get_user_lock

_LOG = logging.getLogger("nimoos-agent.memory_extract")

POLL_SECONDS = 30
IDLE_SECONDS = 120
MAX_ATTEMPTS = 3
LLM_TIMEOUT = 60
HISTORY_MAX_CHARS = 12000


def maybe_enqueue_extract_job(conn, session_id, user_id, *, provider_url,
                              provider_key, provider_type, model_name,
                              now=None) -> bool:
    """UPSERT a per-session extraction job (coalescing) iff memory is enabled
    for the user. Refreshes the credential snapshot + enqueued_at. Returns
    True when a job is (re)enqueued."""
    if not memory_store.is_memory_enabled(conn, user_id):
        return False
    now = now if now is not None else int(time.time())
    conn.execute(
        "INSERT INTO memory_extract_jobs "
        "(session_id, user_id, status, attempts, provider_url, provider_key, "
        " provider_type, model_name, last_error, enqueued_at, updated_at) "
        "VALUES (?,?, 'pending', 0, ?,?,?,?, NULL, ?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        " status='pending', attempts=0, provider_url=excluded.provider_url, "
        " provider_key=excluded.provider_key, provider_type=excluded.provider_type, "
        " model_name=excluded.model_name, last_error=NULL, "
        " enqueued_at=excluded.enqueued_at, updated_at=excluded.updated_at",
        (session_id, str(user_id), provider_url, provider_key, provider_type,
         model_name, now, now),
    )
    conn.commit()
    return True


_VALID_KINDS = ("preference", "fact", "goal")
_EXTRACT_INSTRUCTIONS = (
    "你是 NimoOS 的记忆维护器。读「现有记忆」与「对话」,只抽取关于用户的**持久**"
    "偏好/事实/目标(忽略一次性任务细节、临时上下文)。对每条产出一个动作,并列出"
    "本次对话实际涉及到的现有记忆 id。严格输出 JSON,无多余文字:\n"
    '{"actions":[{"op":"ADD","kind":"preference|fact|goal","text":"一句话","priority":0},'
    '{"op":"UPDATE","id":"<现有id>","kind":"...","text":"新的一句话"},'
    '{"op":"NOOP","id":"<现有id>"}],"referenced":["<现有id>"]}\n'
    "ADD=新事实;UPDATE=新事实取代某条旧记忆(如改了偏好);NOOP=对话印证了旧记忆但无变化。"
    "没有可记的就输出 {\"actions\":[],\"referenced\":[]}。不要产生删除动作。"
)


def build_extraction_prompt(history, existing) -> str:
    existing_lines = "\n".join(
        f'- id={e.get("id")} [{e.get("kind")}] {e.get("text")}' for e in existing
    ) or "(无)"
    convo = json.dumps(history, ensure_ascii=False)
    if len(convo) > HISTORY_MAX_CHARS:
        convo = convo[-HISTORY_MAX_CHARS:]
    return (f"{_EXTRACT_INSTRUCTIONS}\n\n## 现有记忆\n{existing_lines}\n\n"
            f"## 对话\n{convo}")


def _clean_json_text(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.endswith("```"):
            t = t[: -3]
    return t.strip()


def parse_extraction(text):
    try:
        obj = json.loads(_clean_json_text(text))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    actions = []
    for a in obj.get("actions", []) if isinstance(obj.get("actions"), list) else []:
        if not isinstance(a, dict):
            continue
        op = a.get("op")
        if op == "ADD":
            kind, txt = a.get("kind"), a.get("text")
            if kind in _VALID_KINDS and isinstance(txt, str) and txt.strip():
                actions.append({"op": "ADD", "id": None, "kind": kind,
                                "text": txt.strip(),
                                "priority": a.get("priority", 0)
                                if isinstance(a.get("priority", 0), int) else 0})
        elif op == "UPDATE":
            kind, txt, mid = a.get("kind"), a.get("text"), a.get("id")
            if (isinstance(mid, str) and mid and kind in _VALID_KINDS
                    and isinstance(txt, str) and txt.strip()):
                actions.append({"op": "UPDATE", "id": mid, "kind": kind,
                                "text": txt.strip(),
                                "priority": a.get("priority", 0)
                                if isinstance(a.get("priority", 0), int) else 0})
        elif op == "NOOP":
            mid = a.get("id")
            if isinstance(mid, str) and mid:
                actions.append({"op": "NOOP", "id": mid})
    ref_raw = obj.get("referenced", [])
    referenced = [r for r in ref_raw if isinstance(r, str)] if isinstance(ref_raw, list) else []
    return {"actions": actions, "referenced": referenced}


def _current(conn, mem_id, user_id):
    return conn.execute(
        "SELECT updated_at FROM memory_entries "
        "WHERE id=? AND user_id=? AND status='active'",
        (mem_id, str(user_id)),
    ).fetchone()


def apply_extraction(conn, user_id, snapshot, result, *, now=None) -> dict:
    now = now if now is not None else int(time.time())
    counts = {"added": 0, "updated": 0, "noop": 0, "referenced": 0, "skipped": 0}
    for a in result.get("actions", []):
        op = a["op"]
        if op == "ADD":
            if memory_store.find_active_duplicate(conn, user_id, a["text"]):
                counts["skipped"] += 1
                continue
            memory_store.add_memory(conn, user_id, a["text"], a["kind"],
                                    source="auto", priority=a.get("priority", 0),
                                    now=now)
            counts["added"] += 1
        elif op in ("UPDATE", "NOOP"):
            mid = a["id"]
            row = _current(conn, mid, user_id)
            # optimistic: target must be unchanged since the snapshot
            if row is None or mid not in snapshot or row["updated_at"] != snapshot[mid]:
                counts["skipped"] += 1
                continue
            if op == "UPDATE":
                new_id = memory_store.supersede_memory(
                    conn, mid, user_id, a["text"], a["kind"],
                    priority=a.get("priority", 0), now=now)
                counts["updated" if new_id else "skipped"] += 1
            else:  # NOOP — touch updated_at so it sorts as recently seen
                conn.execute(
                    "UPDATE memory_entries SET updated_at=? WHERE id=?", (now, mid))
                conn.commit()
                counts["noop"] += 1
    ref_ids = [r for r in result.get("referenced", [])
               if _current(conn, r, user_id) is not None]
    if ref_ids:
        memory_store.bump_recall(conn, ref_ids, now=now)
        counts["referenced"] = len(ref_ids)
    return counts


def _claim_idle_job(conn, now):
    """Pick the oldest pending job whose session has been idle >= IDLE_SECONDS.
    Returns the row (sqlite3.Row) or None."""
    return conn.execute(
        "SELECT * FROM memory_extract_jobs "
        "WHERE status='pending' AND enqueued_at <= ? "
        "ORDER BY enqueued_at ASC LIMIT 1",
        (now - IDLE_SECONDS,),
    ).fetchone()


def _fail_job(conn, session_id, attempts, err, now):
    if attempts >= MAX_ATTEMPTS:
        conn.execute("DELETE FROM memory_extract_jobs WHERE session_id=?", (session_id,))
    else:
        conn.execute(
            "UPDATE memory_extract_jobs SET status='pending', last_error=?, updated_at=? "
            "WHERE session_id=?", (err, now, session_id))
    conn.commit()


async def process_pending_once(conn, *, llm_call, history_loader, now=None):
    now = now if now is not None else int(time.time())
    job = _claim_idle_job(conn, now)
    if job is None:
        return None
    session_id, user_id = job["session_id"], job["user_id"]
    attempts = job["attempts"] + 1
    conn.execute("UPDATE memory_extract_jobs SET status='running', attempts=?, updated_at=? "
                 "WHERE session_id=?", (attempts, now, session_id))
    conn.commit()

    lock = get_user_lock(user_id)
    # 1) snapshot existing memories under the lock (short)
    async with lock:
        rows = memory_store.list_active(conn, user_id, now=now)
        existing = [{"id": r["id"], "kind": r["kind"], "text": r["text"]} for r in rows]
        snapshot = {r["id"]: r["updated_at"] for r in rows}

    # 2) LLM call OUTSIDE the lock, hard-bounded
    try:
        history = history_loader(session_id)
        prompt = build_extraction_prompt(history, existing)
        raw = await asyncio.wait_for(llm_call(job, prompt), timeout=LLM_TIMEOUT)
        result = parse_extraction(raw)
        if result is None:
            raise ValueError("unparseable extraction response")
    except Exception as e:                       # timeout / network / parse / creds
        _LOG.warning("memory extract failed for %s: %s", session_id, e)
        _fail_job(conn, session_id, attempts, str(e), now)
        return session_id

    # 3) apply under the lock (short), then delete the job row
    async with lock:
        mx_counts = apply_extraction(conn, user_id, snapshot, result, now=now)
    conn.execute("DELETE FROM memory_extract_jobs WHERE session_id=?", (session_id,))
    conn.commit()
    _LOG.info("memory extract %s: %s", session_id, mx_counts)
    return session_id


async def worker_loop(conn, *, stop_event):
    """Poll loop; one job per tick (sequential, CPU-NAS friendly)."""
    while not stop_event.is_set():
        try:
            await process_pending_once(conn, llm_call=_default_llm_call,
                                       history_loader=_default_history_loader)
        except Exception as e:                   # never let the loop die
            _LOG.exception("memory worker tick error: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_SECONDS)
        except asyncio.TimeoutError:
            pass
