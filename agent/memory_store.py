"""Profile-layer memory store: cross-session user facts injected into the
system prompt each turn. Pure SQLite + arithmetic — no embeddings, no model
calls. Every function takes an explicit sqlite3.Connection so the store is
trivially unit-testable. (Recall/extraction/compaction live in other modules.)
"""
from __future__ import annotations

import math
import re
import time
import uuid

VALID_KINDS = ("preference", "fact", "goal")

_WS = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    return _WS.sub(" ", text.strip().lower())


def add_memory(conn, user_id, text, kind="fact", *, source="auto",
               priority=0, origin_session_id=None, lineage_id=None,
               expires_at=None, now=None) -> str:
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid kind: {kind}")
    now = now if now is not None else int(time.time())
    mem_id = uuid.uuid4().hex
    lineage = lineage_id or mem_id
    conn.execute(
        "INSERT INTO memory_entries "
        "(id, user_id, kind, text, source, priority, status, lineage_id, "
        " supersedes, recall_count, last_recalled_at, created_at, updated_at, "
        " expires_at, origin_session_id) "
        "VALUES (?,?,?,?,?,?,'active',?,NULL,0,?,?,?,?,?)",
        (mem_id, user_id, kind, text, source, priority, lineage,
         now, now, now, expires_at, origin_session_id),
    )
    conn.commit()
    return mem_id


def find_active_duplicate(conn, user_id, text):
    norm = normalize_text(text)
    for row in conn.execute(
            "SELECT id, text FROM memory_entries "
            "WHERE user_id=? AND status='active'", (user_id,)):
        if normalize_text(row["text"]) == norm:
            return row["id"]
    return None


def list_active(conn, user_id, *, now=None):
    now = now if now is not None else int(time.time())
    return conn.execute(
        "SELECT * FROM memory_entries "
        "WHERE user_id=? AND status='active' "
        "AND (expires_at IS NULL OR expires_at > ?)",
        (user_id, now),
    ).fetchall()


def disable_memory(conn, mem_id) -> int:
    cur = conn.execute(
        "UPDATE memory_entries SET status='disabled', updated_at=? "
        "WHERE id=? AND status='active'",
        (int(time.time()), mem_id),
    )
    conn.commit()
    return cur.rowcount


def disable_by_text(conn, user_id, query):
    norm = normalize_text(query)
    ids = [r["id"] for r in conn.execute(
        "SELECT id, text FROM memory_entries "
        "WHERE user_id=? AND status='active'", (user_id,))
        if norm in normalize_text(r["text"])]
    for mid in ids:
        disable_memory(conn, mid)
    return ids


def effective_score(row, now) -> float:
    age_days = max(0.0, (now - row["last_recalled_at"]) / 86400.0)
    recency_boost = 2.0 / (1.0 + age_days / 30.0)
    reinforcement = math.log1p(row["recall_count"])
    return float(row["priority"]) + recency_boost + reinforcement


def rank_for_injection(rows, now) -> list:
    return sorted(rows, key=lambda r: effective_score(r, now), reverse=True)


MAX_INJECT_ENTRIES = 30
MAX_INJECT_CHARS = 4000  # ~1500 tokens, conservative. P4 introduces the shared estimator.


def render_user_block(conn, user_id, *, now=None,
                      max_entries=MAX_INJECT_ENTRIES,
                      max_chars=MAX_INJECT_CHARS) -> str:
    now = now if now is not None else int(time.time())
    rows = list_active(conn, user_id, now=now)
    if not rows:
        return ""
    ranked = rank_for_injection(rows, now)
    lines = []
    used = 0
    for r in ranked[:max_entries]:
        line = f"- ({r['kind']}) {r['text']}"
        if lines and used + len(line) > max_chars:
            break
        if not lines and len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
    return "## 关于这位用户\n\n" + "\n".join(lines)


def bump_recall(conn, ids, *, now=None) -> None:
    if not ids:
        return
    now = now if now is not None else int(time.time())
    conn.executemany(
        "UPDATE memory_entries SET recall_count = recall_count + 1, "
        "last_recalled_at=? WHERE id=?",
        [(now, i) for i in ids],
    )
    conn.commit()
