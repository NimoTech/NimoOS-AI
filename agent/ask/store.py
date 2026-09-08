"""ask_turns persistence (spec §3.3.6)."""
from __future__ import annotations

import json
import time
import uuid


def insert_turn(conn, *, session_id: str, run_id: str, question: str,
                plan: dict, sources: list[dict], stages: list[dict]) -> str:
    tid = "ask_" + uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO ask_turns (id, session_id, run_id, question, plan_json, sources_json, stages_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (tid, session_id, run_id, question, json.dumps(plan, ensure_ascii=False),
         json.dumps(sources, ensure_ascii=False), json.dumps(stages, ensure_ascii=False), int(time.time())))
    conn.commit()
    return tid


def _loads(raw, default):
    try:
        v = json.loads(raw) if raw else default
    except (TypeError, json.JSONDecodeError):
        return default
    return v if isinstance(v, type(default)) else default


def list_turns(conn, session_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, run_id, question, plan_json, sources_json, stages_json, created_at "
        "FROM ask_turns WHERE session_id=? ORDER BY created_at ASC, rowid ASC", (session_id,)).fetchall()
    return [{"id": r["id"], "run_id": r["run_id"], "question": r["question"],
             "plan": _loads(r["plan_json"], {}), "sources": _loads(r["sources_json"], []),
             "stages": _loads(r["stages_json"], []), "created_at": int(r["created_at"])} for r in rows]


def seen_keys(conn, session_id: str) -> set[str]:
    keys: set[str] = set()
    for turn in list_turns(conn, session_id):
        for s in turn["sources"]:
            if s.get("source") == "note":
                keys.add(f"note:{s.get('note_id') or str(s.get('file_id', '')).removeprefix('note:')}:{int(s.get('chunk_no') or 0)}")
                continue
            fid, kind = s.get("file_id", ""), s.get("kind", "body")
            for cn in (s.get("merged_chunk_nos") or [s.get("chunk_no", 0)]):
                keys.add(f"doc:{fid}:{kind}:{int(cn)}")
    return keys


def recent_questions(conn, session_id: str, n: int = 2) -> str:
    rows = conn.execute(
        "SELECT question FROM ask_turns WHERE session_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (session_id, n)).fetchall()
    qs = [r["question"] for r in reversed(rows)]
    return "\n".join(f"Q{i + 1}: {q}" for i, q in enumerate(qs))
