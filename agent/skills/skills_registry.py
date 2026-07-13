"""Skill read tools + system-prompt index for the agent.

`render_index_block()` renders the <available-skills> index injected into
the system prompt each run (L1 progressive disclosure).
`read_skill_file()` reads a file inside a skill bundle (SKILL.md by
default).

Both scan /<root>/.runtime/<user_id>/, the symlink tree maintained by the
Go service. They bypass the generic `read_file` fs policy on purpose:
skill bundles are not user-shared resources, so the per-session
`visible_resources` gate would reject them, and `/var/lib/nimoos/` is on
the hard blacklist. Access is still locked down — only paths inside the
current user's runtime view are allowed.
"""
from __future__ import annotations

import json
import logging
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

_log = logging.getLogger(__name__)

_INDEX_HEADER = (
    "<available-skills>\n"
    "The user has installed the following skills — named procedures with\n"
    "detailed instructions. When a request matches a skill's description,\n"
    "FIRST call read_skill_file(skill_id) to load its SKILL.md instructions,\n"
    "then follow them. Users may also invoke a skill explicitly by typing\n"
    "/<skill-id> in their message.\n\n"
)
_INDEX_FOOTER = "</available-skills>"
_MAX_INDEX_BYTES = 16 * 1024
_MAX_DESC_CHARS = 256
_WS_RE = re.compile(r"[\r\n\t]")
_BAD_RE = re.compile(r"[\x00-\x1f\x7f<>]")


def _sanitize_description(desc) -> str:
    """Defense-in-depth cleaning for descriptions injected into the system
    prompt. Covers builtin manifests and pre-existing bundles that never
    went through the Go upload validation."""
    text = _WS_RE.sub(" ", str(desc))
    text = _BAD_RE.sub("", text)
    return " ".join(text.split())[:_MAX_DESC_CHARS]


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


def render_index_block() -> str:
    """Render the <available-skills> system-prompt block (L1 progressive
    disclosure). Empty string when the user has no visible (auto/slash)
    skills or the runtime view is unreadable. Never raises: prompt
    composition must not fail because of bad skill data."""
    try:
        visible = [s for s in _scan_runtime_view()
                   if s.get("trigger") != "manual"
                   and _SKILL_ID_RE.match(str(s.get("skill_id", "")))]
        if not visible:
            return ""
        visible.sort(key=lambda s: s["skill_id"])
        used = len(_INDEX_HEADER.encode()) + len(_INDEX_FOOTER.encode())
        # Reserve worst-case room for the omitted-notice line so the final
        # block never exceeds _MAX_INDEX_BYTES even when it is appended.
        notice_reserve = len(
            (f"[{len(visible)} more skills omitted — disable unused "
             "skills in Settings]\n").encode())
        lines: list[str] = []
        omitted = 0
        for i, s in enumerate(visible):
            entry = (f"- {s['skill_id']}: "
                     f"{_sanitize_description(s.get('description', ''))}\n")
            if used + len(entry.encode()) > _MAX_INDEX_BYTES - notice_reserve:
                omitted = len(visible) - i
                break
            lines.append(entry)
            used += len(entry.encode())
        if omitted:
            lines.append(f"[{omitted} more skills omitted — disable unused "
                         "skills in Settings]\n")
        return _INDEX_HEADER + "".join(lines) + _INDEX_FOOTER
    except Exception:
        _log.warning("render_index_block failed", exc_info=True)
        return ""


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
    with a skill. `skill_id` is the id shown in the <available-skills>
    index in your system prompt.
    `path` is relative to the bundle root and cannot escape the bundle.

    Returns the file contents as a string, or an `Error: ...` message on
    invalid id, missing skill, traversal attempt, or oversized file.
    """
    return _read_skill_file(skill_id, path)


ALL_TOOLS = [read_skill_file]
