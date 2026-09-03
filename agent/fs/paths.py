"""Path resolution + scope guard for fs tools.

resolve(raw, session_id, db) returns the canonical absolute path or raises
PermissionDenied. The result has been:
  1. abspath'd (relative paths anchored to the session's single visible folder)
  2. realpath'd (symlinks expanded)
  3. validated against the session's visible_resources rows
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

import tool_output as _tool_output


class PermissionDenied(Exception):
    pass


@dataclass(frozen=True)
class _VR:
    path: str
    kind: str  # 'folder' | 'file'


def _load_visible(db: sqlite3.Connection, session_id: str) -> list[_VR]:
    rows = db.execute(
        "SELECT path, kind FROM visible_resources WHERE session_id=?",
        (session_id,),
    ).fetchall()
    return [_VR(r["path"], r["kind"]) for r in rows]


def _is_within(child: str, parent: str) -> bool:
    parent = parent.rstrip(os.sep)
    return child == parent or child.startswith(parent + os.sep)


def anchor(raw: str, session_id: str, db: sqlite3.Connection) -> str:
    """Absolutize `raw` WITHOUT scope-checking. Relative paths anchor to the
    session's single visible folder (matching resolve()). Raises
    PermissionDenied for null bytes or un-anchorable relative paths."""
    if not isinstance(raw, str) or "\x00" in raw:
        raise PermissionDenied("path contains null byte or is not a string")
    visible = _load_visible(db, session_id)
    if not os.path.isabs(raw):
        folders = [v for v in visible if v.kind == "folder"]
        if len(folders) != 1:
            raise PermissionDenied(
                "relative path not allowed when 0 or >1 visible folders; "
                "use an absolute path"
            )
        anchored = os.path.join(folders[0].path, raw)
    else:
        anchored = raw
    return os.path.abspath(anchored)


def resolve(raw: str, session_id: str, db: sqlite3.Connection) -> str:
    if not isinstance(raw, str) or "\x00" in raw:
        raise PermissionDenied("path contains null byte or is not a string")

    # Tool-output offload folder (tool_output.py, spec §4.2): this session's
    # own scratch, implicitly in scope. Absolute paths only — it never anchors
    # relative paths and never appears in visible_resources, so a chat with one
    # picked folder keeps its single-folder relative-path anchoring.
    if os.path.isabs(raw):
        real0 = os.path.realpath(raw)
        own = os.path.realpath(_tool_output.chat_dir_for_session(session_id))
        if real0 == own or real0.startswith(own + os.sep):
            return real0

    visible = _load_visible(db, session_id)
    if not visible:
        raise PermissionDenied("no authorized resources for this session")

    # 1. Absolutize / anchor (shared with _candidate_for_request).
    abs_ = anchor(raw, session_id, db)

    # 2. Realpath. We resolve symlinks even on non-existent leaves: the parent
    #    chain must already canonicalize to inside scope. os.path.realpath
    #    handles non-existent tails gracefully on POSIX.
    real = os.path.realpath(abs_)

    # 3. Scope check.
    for v in visible:
        v_real = os.path.realpath(v.path)
        if v.kind == "folder":
            if _is_within(real, v_real):
                return real
        else:  # file
            if real == v_real:
                return real
    raise PermissionDenied(f"{raw!r} is not within any visible resource")
