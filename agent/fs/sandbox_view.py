"""Compute the read-only sandbox view for the shell tool.

Mirrors each authorized resource (visible_resources) into the bwrap sandbox at
its real path, READ-ONLY. No in-tree masking: the shell sees the authorized
folders exactly as they are. Defense remains: the file tools still gate per
read, the sandbox is read-only (--ro-bind) and offline by default, and the
resources were explicitly authorized by the user.

The one retained check: an authorized resource whose OWN path is blocked by a
non-overridable layer (implicit-ignore / builtin or user hard blacklist) is not
mounted at all — consistent with the file tools' treatment of that path, and
guarding against "authorized first, blacklisted later" drift. This is a single
match per resource, NOT a tree walk. Pure logic, fully unit-testable.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field

from . import ignore


@dataclass
class SandboxView:
    ro_binds: list[tuple[str, str]] = field(default_factory=list)  # (src, dst), mirrored real paths
    skipped: list[str] = field(default_factory=list)               # resources NOT mounted (own path gated)


def _is_gated(abs_path: str, user_patterns: list[str]) -> bool:
    """True if abs_path's OWN path is blocked by a non-overridable layer
    (implicit-ignore or builtin/user hard blacklist). gitignore is overridden
    (not a secret boundary). visible_roots is irrelevant here (only used by the
    gitignore layer), so we pass []."""
    try:
        ignore.gate(abs_path, [], user_patterns, allow_gitignore_override=True)
        return False
    except (ignore.BlockedImplicit, ignore.BlockedHardBlacklist):
        return True


def _load_visible(db: sqlite3.Connection, session_id: str) -> list[tuple[str, str]]:
    rows = db.execute(
        "SELECT path, kind FROM visible_resources WHERE session_id=?",
        (session_id,),
    ).fetchall()
    return [(r["path"], r["kind"]) for r in rows]


def build_view(session_id: str, db: sqlite3.Connection,
               user_patterns: list[str]) -> SandboxView:
    view = SandboxView()
    visible = _load_visible(db, session_id)
    resolved = [(os.path.realpath(p), k) for p, k in visible]

    # Dedup nested folders: keep only the outermost.
    folder_reals = sorted((r for r, k in resolved if k == "folder"), key=len)
    kept: list[str] = []
    for r in folder_reals:
        if any(r == p or r.startswith(p + os.sep) for p in kept):
            continue
        kept.append(r)

    # Single authorized files not already covered by a kept folder.
    for real, kind in resolved:
        if kind == "file" and not any(
                real == p or real.startswith(p + os.sep) for p in kept):
            if _is_gated(real, user_patterns):
                view.skipped.append(real)
                continue
            view.ro_binds.append((real, real))

    # Authorized folders — mounted whole, read-only, NO in-tree masking.
    for folder in kept:
        if _is_gated(folder + os.sep, user_patterns):  # gate the root itself
            view.skipped.append(folder)
            continue
        view.ro_binds.append((folder, folder))
    return view


def to_bwrap_args(view: SandboxView) -> list[str]:
    """Flatten the view into bwrap args: one --ro-bind per authorized resource."""
    args: list[str] = []
    for src, dst in view.ro_binds:
        args += ["--ro-bind", src, dst]
    return args
