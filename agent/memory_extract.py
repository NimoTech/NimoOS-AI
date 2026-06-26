"""Session-idle memory auto-extraction (P3). A startup asyncio worker, after a
session goes quiet, asks the conversation's own model to distill durable user
facts and applies ADD/UPDATE/NOOP to the profile store. Fully async — never on
the chat main path; bounded by hard timeout + attempt cap + sequential single-flight.
"""
from __future__ import annotations

import json
import time

import memory_store

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
