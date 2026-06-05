"""Compute the read-only sandbox view for the shell tool.

Mirrors each authorized resource (visible_resources) into the bwrap sandbox at
its real path, then masks any subpath the hard ignore layers block — so `cat`/
`grep` see exactly what the file tools allow. Pure logic: no bwrap, no I/O
beyond stat/scandir, fully unit-testable.

Masking uses gate(..., allow_gitignore_override=True): only the non-overridable
layers (implicit-ignore + builtin hard blacklist + user patterns) are masked.
.gitignore is NOT masked — it's noise filtering, not a secret boundary, and the
whole point of shell grep is to search project files.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field

from . import ignore

# Tunables (kept small so the bwrap arg set stays bounded; see spec §4.2/§4.3).
MAX_ENTRIES = 2000      # total entries examined per authorized folder
FOLD_THRESHOLD = 200    # masked files in one dir → fold the whole dir to --tmpfs


@dataclass
class SandboxView:
    ro_binds: list[tuple[str, str]] = field(default_factory=list)  # (src, dst), mirrored
    dir_masks: list[str] = field(default_factory=list)             # --tmpfs <path>
    file_masks: list[str] = field(default_factory=list)            # --ro-bind /dev/null <path>
    skipped: list[str] = field(default_factory=list)               # folded/over-budget subtrees


def _is_masked(abs_path: str, roots: list[str], user_patterns: list[str]) -> bool:
    try:
        ignore.gate(abs_path, roots, user_patterns, allow_gitignore_override=True)
        return False
    except (ignore.BlockedImplicit, ignore.BlockedHardBlacklist):
        return True


def _load_visible(db: sqlite3.Connection, session_id: str) -> list[tuple[str, str]]:
    rows = db.execute(
        "SELECT path, kind FROM visible_resources WHERE session_id=?",
        (session_id,),
    ).fetchall()
    return [(r["path"], r["kind"]) for r in rows]


def _walk_dir(d: str, roots: list[str], user_patterns: list[str],
              view: SandboxView, budget: list[int]) -> bool:
    """Populate view for directory `d`.

    Return True if `d`'s own entries were fully classified within budget
    (descendants may have been folded); False ONLY if budget ran out during
    THIS dir's own entry scan, in which case the caller must fold all of `d`.
    """
    try:
        entries = sorted(os.scandir(d), key=lambda e: e.name)
    except OSError:
        return True  # unreadable: nothing to expose
    local_dir_masks: list[str] = []
    local_file_masks: list[str] = []
    sub_dirs: list[str] = []
    for e in entries:
        if budget[0] <= 0:
            return False  # truncated → caller folds whole `d`
        budget[0] -= 1
        path = os.path.join(d, e.name)
        # Symlinks are NOT followed (follow_symlinks=False): a symlink is probed
        # as a plain file, so a symlink named e.g. ".ssh" is not caught by the
        # directory-only ".ssh/" pattern. This is the same path-based-masking
        # limitation as hardlinks (see spec §7) and matches the file tools' own
        # gate(); a symlink only reads its target if that target is itself
        # mounted (i.e. already in authorized scope or a read-only system dir).
        try:
            is_dir = e.is_dir(follow_symlinks=False)
        except OSError:
            is_dir = False
        check_path = path + os.sep if is_dir else path
        if _is_masked(check_path, roots, user_patterns):
            (local_dir_masks if is_dir else local_file_masks).append(path)
        elif is_dir:
            sub_dirs.append(path)
        # plain visible file: exposed by parent ro-bind, nothing to emit
    if len(local_file_masks) > FOLD_THRESHOLD:
        view.dir_masks.append(d)      # fold entire dir
        view.skipped.append(d)
        return True
    view.dir_masks.extend(local_dir_masks)
    view.file_masks.extend(local_file_masks)
    for i, sd in enumerate(sub_dirs):
        if not _walk_dir(sd, roots, user_patterns, view, budget):
            # sd's own scan was truncated by budget. Fold sd AND every
            # not-yet-walked sibling, so nothing under `d` is left bound but
            # unmasked (budget is now exhausted; the remaining siblings would
            # not be analyzed). `d` itself is fully accounted for, so return
            # True — `d`'s already-masked entries stay granularly visible.
            for rest in sub_dirs[i:]:
                view.dir_masks.append(rest)
                view.skipped.append(rest)
            return True
    return True


def build_view(session_id: str, db: sqlite3.Connection,
               user_patterns: list[str]) -> SandboxView:
    view = SandboxView()
    visible = _load_visible(db, session_id)
    resolved = [(os.path.realpath(p), k) for p, k in visible]

    # Dedup nested folders: keep only outermost.
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
            # Gate the file itself: if a (user) blacklist pattern matches it,
            # do not expose it — keeps shell consistent with the file tools,
            # which re-gate on every read.
            if _is_masked(real, kept, user_patterns):
                view.skipped.append(real)
                continue
            view.ro_binds.append((real, real))

    for folder in kept:
        # Gate the folder root itself (trailing sep for dir-pattern matching).
        if _is_masked(folder + os.sep, kept, user_patterns):
            view.skipped.append(folder)
            continue
        view.ro_binds.append((folder, folder))
        if not _walk_dir(folder, kept, user_patterns, view, [MAX_ENTRIES]):
            # The root's OWN entry scan was truncated: its contents were not
            # classified, so we cannot guarantee secrets are masked. Fold the
            # whole root (it shows empty in the shell — file tools still work).
            view.dir_masks.append(folder)
            view.skipped.append(folder)
    return view


def to_bwrap_args(view: SandboxView) -> list[str]:
    """Flatten a view into bwrap args. Binds first, then masks (masks must come
    after their parent ro-bind so bwrap applies them on top)."""
    args: list[str] = []
    for src, dst in view.ro_binds:
        args += ["--ro-bind", src, dst]
    for d in view.dir_masks:
        args += ["--tmpfs", d]
    for f in view.file_masks:
        args += ["--ro-bind", "/dev/null", f]
    return args
