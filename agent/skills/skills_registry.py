"""Skill discovery + read tools for the agent.

`list_skills()` enumerates the user's enabled skills.
`read_skill_file()` reads a file inside a skill bundle (SKILL.md by default).

Both scan /<root>/.runtime/<user_id>/, the symlink tree maintained by the
Go service. They bypass the generic `read_file` fs policy on purpose:
skill bundles are not user-shared resources, so the per-session
`visible_resources` gate would reject them, and `/var/lib/nimoos/` is on
the hard blacklist. Access is still locked down — only paths inside the
current user's runtime view are allowed.
"""
from __future__ import annotations

import json
import os
import re
from contextvars import ContextVar
from pathlib import Path

from agents import function_tool

# Both set by agent.py::run() at the top of each chat turn.
SKILLS_ROOT_VAR: ContextVar[str] = ContextVar(
    "skills_root", default="/var/lib/nimoos/ai/skills")
USER_ID_VAR: ContextVar[str] = ContextVar("user_id", default="")

_SKILL_ID_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")
_MAX_SKILL_FILE_BYTES = 256 * 1024


def _runtime_root() -> Path:
    root = Path(SKILLS_ROOT_VAR.get())
    user = USER_ID_VAR.get()
    return root / ".runtime" / user


def _scan_runtime_view() -> list[dict]:
    rt = _runtime_root()
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
            # Logical id only — actual reading goes through read_skill_file.
            "skill_id": entry.name,
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
    list of {id, name, description, trigger, skill_id}. To read the
    SKILL.md (full instructions) for a given skill, call
    `read_skill_file(skill_id)`.
    """
    return _format_for_llm(_scan_runtime_view())


def _read_skill_file(skill_id: str, path: str) -> str:
    """Pure helper used by the function tool; also covered by tests."""
    if not isinstance(skill_id, str) or not _SKILL_ID_RE.match(skill_id):
        return "Error: invalid skill_id"
    if not isinstance(path, str) or not path:
        return "Error: empty path"
    if "\x00" in path or os.path.isabs(path):
        return "Error: path must be relative and contain no NUL"

    bundle = _runtime_root() / skill_id
    if not bundle.is_dir():
        return f"Error: skill {skill_id!r} not installed or disabled"

    bundle_real = os.path.realpath(bundle)
    target_real = os.path.realpath(bundle / path)
    if not (target_real == bundle_real or
            target_real.startswith(bundle_real + os.sep)):
        return "Error: path escapes the skill bundle"

    if not os.path.isfile(target_real):
        return f"Error: file not found: {path}"

    try:
        size = os.path.getsize(target_real)
    except OSError as e:
        return f"Error: stat failed: {e}"
    if size > _MAX_SKILL_FILE_BYTES:
        return (f"Error: file too large ({size} bytes); skill files capped "
                f"at {_MAX_SKILL_FILE_BYTES}")

    try:
        with open(target_real, "rb") as f:
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except OSError as e:
        return f"Error: read failed: {e}"


@function_tool
async def read_skill_file(skill_id: str, path: str = "SKILL.md") -> str:
    """Read a file from a skill bundle (defaults to SKILL.md).

    Use this to fetch the SKILL.md instructions or any other file shipped
    with a skill. `skill_id` is the value returned by `list_skills`.
    `path` is relative to the bundle root and cannot escape the bundle.

    Returns the file contents as a string, or an `Error: ...` message on
    invalid id, missing skill, traversal attempt, or oversized file.
    """
    return _read_skill_file(skill_id, path)


ALL_TOOLS = [list_skills, read_skill_file]
