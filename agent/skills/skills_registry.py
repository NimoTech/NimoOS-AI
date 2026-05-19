"""list_skills() function tool.

Scans /<root>/.runtime/<user_id>/ (a symlink tree maintained by the Go
service) and returns enabled skills as a JSON list. Each entry tells the
LLM where SKILL.md lives so it can read it via `read_file`.
"""
from __future__ import annotations

import json
import os
from contextvars import ContextVar
from pathlib import Path

from agents import function_tool

# Both set by agent.py::run() at the top of each chat turn.
SKILLS_ROOT_VAR: ContextVar[str] = ContextVar(
    "skills_root", default="/var/lib/nimoos/skills")
USER_ID_VAR: ContextVar[str] = ContextVar("user_id", default="")


def _scan_runtime_view() -> list[dict]:
    root = Path(SKILLS_ROOT_VAR.get())
    user = USER_ID_VAR.get()
    rt = root / ".runtime" / user
    if not rt.is_dir():
        return []
    out = []
    for entry in sorted(rt.iterdir()):
        manifest = entry / "manifest.json"
        if not manifest.is_file():
            continue
        try:
            m = json.loads(manifest.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "id": m.get("id", entry.name),
            "name": m.get("name", entry.name),
            "description": m.get("description", ""),
            "trigger": m.get("trigger", "auto"),
            # Path inside the bwrap sandbox (the agent's CWD)
            "path": f"/skill/{entry.name}",
        })
    return out


def _format_for_llm(skills: list[dict]) -> str:
    """Filter to skills the LLM should know about by default.

    `manual` skills are hidden — they only fire from the UI's "Try in chat"
    button, which is handled by the Go layer pre-injecting SKILL.md.
    """
    visible = [s for s in skills if s.get("trigger") != "manual"]
    return json.dumps(visible, ensure_ascii=False)


@function_tool
async def list_skills() -> str:
    """List installed skills available to this user.

    Use this when the user asks about a capability you might not directly
    have, or when a `/<name>` slash command appears. The result is a JSON
    list of {id, name, description, trigger, path}. For full instructions,
    read `<path>/SKILL.md` with the `read_file` tool.
    """
    return _format_for_llm(_scan_runtime_view())


ALL_TOOLS = [list_skills]
