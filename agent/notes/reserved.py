"""OKF reserved files (index.md / log.md), auto-maintained per user.
Pure template rendering — zero LLM. log.md is BOUNDED (LOG_CAP) by design:
unbounded change journals are how wiki file_events reached 129M rows."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from notes import store

LOG_CAP = 200


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def render_for_user(conn, user_id: str) -> None:
    root = store.get_notes_root(conn)
    udir = os.path.join(root, str(user_id))
    if not os.path.isdir(udir):
        return
    live = conn.execute(
        "SELECT id, path, title, description, type, status, revision, "
        "created_at, updated_at, deleted_at FROM notes WHERE user_id=? "
        "ORDER BY updated_at DESC", (str(user_id),)).fetchall()

    # ---- index.md: grouped, live notes only ----
    groups: dict[str, list] = {}
    for r in live:
        if r["deleted_at"] is None:
            groups.setdefault(r["type"], []).append(r)
    lines = ["---", 'okf_version: "0.1"', "---",
             "# Notes Index", ""]
    for t in ("note", "summary", "insight", "digest"):
        rows = groups.get(t)
        if not rows:
            continue
        lines.append(f"## {t.capitalize()}")
        for r in rows:
            desc = f" - {r['description']}" if r["description"] else ""
            lines.append(f"* [{r['title']}](/{r['path']}){desc}")
        lines.append("")
    store._atomic_write(os.path.join(udir, "index.md"),
                        "\n".join(lines) + "\n")

    # ---- log.md: bounded flat change log, newest first ----
    entries = []
    for r in live[:LOG_CAP]:
        if r["deleted_at"] is not None:
            verb, ts = "Deprecation", r["deleted_at"]
        elif r["revision"] <= 1:
            verb, ts = "Creation", r["created_at"]
        else:
            verb, ts = "Update", r["updated_at"]
        entries.append((ts, verb, r["title"]))
    entries.sort(key=lambda e: e[0], reverse=True)
    lines = ["# Change Log", ""]
    day = None
    for ts, verb, title in entries[:LOG_CAP]:
        d = _day(ts)
        if d != day:
            lines.append(f"## {d}")
            day = d
        lines.append(f"* **{verb}** {title}")
    store._atomic_write(os.path.join(udir, "log.md"),
                        "\n".join(lines) + "\n")
