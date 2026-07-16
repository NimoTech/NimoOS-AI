"""Per-op atomic apply + the unified write pipeline.

Public API surface (called from skills/filesystem.py):
    list_dir, read_file, read_file_lines, write_file, edit_file,
    delete_path, mkdir, rename, glob_files, search_content
"""
from __future__ import annotations

import fnmatch
import json
import mimetypes
import os
import re
import shutil
import stat
import time
from typing import Optional

from audit import audit as _audit
from fs import paths, ignore, ownership, staging, access_request
from fs.snapshots import SnapshotTooLarge


READ_MAX_BYTES = 1 * 1024 * 1024
GLOB_MAX_RESULTS = 500
SEARCH_MAX_RESULTS = 100


# ---------- helpers ----------

def _gate(ctx, abs_path: str) -> None:
    visible_roots = [r["path"] for r in ctx["conn"].execute(
        "SELECT path FROM visible_resources WHERE session_id=? AND kind='folder'",
        (ctx["session_id"],),
    )]
    ignore.gate(abs_path, visible_roots, ctx.get("user_patterns", []))


def _resolve_and_gate(ctx, raw: str) -> str:
    abs_ = paths.resolve(raw, ctx["session_id"], ctx["conn"])
    _gate(ctx, abs_)
    return abs_


def _candidate_for_request(ctx, raw: str) -> tuple[str, str]:
    """(path, kind) to request authorization for, using the SAME anchoring as
    paths.resolve. Raises paths.PermissionDenied if un-anchorable (relative
    path with 0 or >1 visible folders)."""
    real = os.path.realpath(paths.anchor(raw, ctx["session_id"], ctx["conn"]))
    if os.path.isdir(real):
        return real, "folder"
    if os.path.isfile(real):
        return real, "file"
    # Non-existent (creation/organize target): request the nearest existing
    # ancestor directory as a folder grant.
    parent = os.path.dirname(real)
    while parent and not os.path.isdir(parent):
        parent = os.path.dirname(parent)
    return (parent or "/"), "folder"


async def _resolve_and_gate_or_request(ctx, raw: str, op: str) -> str:
    try:
        return _resolve_and_gate(ctx, raw)
    except paths.PermissionDenied:
        # Out of scope. Decide what to request — but NEVER offer to grant a
        # hard-blacklisted/implicitly-ignored path. paths.resolve runs BEFORE
        # ignore.gate, so blacklisted out-of-scope paths surface here as a
        # PermissionDenied; we must re-run gate explicitly before prompting.
        abs_, kind = _candidate_for_request(ctx, raw)
        # Never grant the filesystem root itself.
        if abs_ == os.sep:
            raise
        visible_roots = [r["path"] for r in ctx["conn"].execute(
            "SELECT path FROM visible_resources WHERE session_id=? AND kind='folder'",
            (ctx["session_id"],))]
        # Hard-blacklist patterns are directory-anchored (e.g. "/etc/") and
        # match a directory's *children*, not the bare directory path. For a
        # folder candidate, also gate a synthetic child so a request for the
        # blacklisted directory itself is caught by the same proven matcher.
        gate_targets = [abs_]
        if kind == "folder":
            gate_targets.append(os.path.join(abs_, "__nimoos_access_probe__"))
        for target in gate_targets:
            ignore.gate(target, visible_roots, ctx.get("user_patterns", []))  # Blocked* → no card
        if ctx.get("confirm_mgr") is None:
            raise  # no interactive channel; behave as before
        granted = await access_request.request_access(ctx, abs_, kind, op)
        if not granted:
            raise paths.PermissionDenied(f"用户拒绝了对 {abs_} 的访问")
        return _resolve_and_gate(ctx, raw)


def _next_seq(ctx) -> int:
    row = ctx["conn"].execute(
        "SELECT COALESCE(MAX(seq), 0) AS m FROM staged_changes "
        "WHERE session_id=? AND run_id=?",
        (ctx["session_id"], ctx["run_id"]),
    ).fetchone()
    return (row["m"] or 0) + 1


async def _emit_staged(ctx, seq: int, op: str, path: str, *,
                       dst_path: Optional[str] = None,
                       size_bytes: int = 0) -> None:
    await ctx["sink"].put({
        "type": "staged_change",
        "run_id": ctx["run_id"],
        "seq": seq,
        "op": op,
        "path": path,
        "dst_path": dst_path,
        "size_bytes": size_bytes,
    })
    try:
        _audit("fs_change", user_id=ctx.get("user_id"),
               session_id=ctx.get("session_id"), op=op, path=path,
               dst_path=dst_path)
    except Exception:  # noqa: BLE001 — audit must never break the fs op
        pass


def _binary_marker(abs_path: str) -> Optional[str]:
    mime, _ = mimetypes.guess_type(abs_path)
    if mime and not mime.startswith("text/") and mime != "application/json":
        size = os.path.getsize(abs_path)
        return f"<binary file: mime={mime}, size={size} bytes>"
    # Fallback: sniff first 4 KiB for NUL byte
    try:
        with open(abs_path, "rb") as f:
            head = f.read(4096)
        if b"\x00" in head:
            return f"<binary file: size={os.path.getsize(abs_path)} bytes>"
    except OSError:
        pass
    return None


def _err(ex: Exception) -> str:
    return f"Error: {ex}"


# ---------- read tools ----------

async def list_dir(ctx, path: str) -> str:
    try:
        abs_ = await _resolve_and_gate_or_request(ctx, path, "list")
    except (paths.PermissionDenied,
            ignore.BlockedImplicit, ignore.BlockedHardBlacklist,
            ignore.BlockedGitignore) as e:
        return _err(e)
    if not os.path.isdir(abs_):
        return f"Error: not a directory: {abs_}"
    visible_roots = [r["path"] for r in ctx["conn"].execute(
        "SELECT path FROM visible_resources WHERE session_id=? AND kind='folder'",
        (ctx["session_id"],),
    )]
    out = []
    for entry in sorted(os.scandir(abs_), key=lambda e: e.name):
        try:
            ignore.gate(entry.path, visible_roots, ctx.get("user_patterns", []))
        except (ignore.BlockedImplicit, ignore.BlockedHardBlacklist,
                ignore.BlockedGitignore):
            continue
        st = entry.stat(follow_symlinks=False)
        out.append({
            "name": entry.name,
            "kind": "dir" if entry.is_dir(follow_symlinks=False) else "file",
            "size": st.st_size,
            "modified": int(st.st_mtime),
        })
    return json.dumps(out, ensure_ascii=False)


async def read_file(ctx, path: str) -> str:
    try:
        abs_ = await _resolve_and_gate_or_request(ctx, path, "read")
    except Exception as e:
        return _err(e)
    if not os.path.isfile(abs_):
        return f"Error: not a file: {abs_}"
    size = os.path.getsize(abs_)
    if size > READ_MAX_BYTES:
        return (f"<file too large: {size} bytes; "
                f"use read_file_lines or edit_file>")
    bm = _binary_marker(abs_)
    if bm:
        return bm
    with open(abs_, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


async def read_file_lines(ctx, path: str, start: int, end: int) -> str:
    try:
        abs_ = await _resolve_and_gate_or_request(ctx, path, "read")
    except Exception as e:
        return _err(e)
    if end - start > 2000:
        return "Error: range too large; max 2000 lines"
    if start < 1 or end < start:
        return "Error: invalid range"
    out: list[str] = []
    with open(abs_, "r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if i >= end:
                break
            if i >= start:
                out.append(line)
    return "".join(out)


# ---------- write tools ----------

async def write_file(ctx, path: str, content: str) -> str:
    try:
        abs_ = await _resolve_and_gate_or_request(ctx, path, "write")
    except Exception as e:
        return _err(e)
    seq = _next_seq(ctx)
    pre_existing = os.path.exists(abs_)
    snap_path = None
    snap_kind = None
    orig_uid = orig_gid = orig_mode = None
    if pre_existing:
        try:
            snap_path = staging.maybe_take_file_snapshot(
                ctx["conn"], ctx["store"], ctx["session_id"],
                ctx["run_id"], str(seq), abs_)
            snap_kind = "file"
            st = os.stat(abs_)
            orig_uid, orig_gid = st.st_uid, st.st_gid
            orig_mode = st.st_mode & 0o777
        except SnapshotTooLarge as e:
            return _err(e)
    # Atomic write: tmp + rename
    tmp = abs_ + ".__nimoos_agent_tmp__"
    os.makedirs(os.path.dirname(abs_), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, abs_)
    ownership.apply(abs_, ctx["chat_username"])
    staging.record(ctx["conn"], ctx["session_id"], ctx["run_id"], seq,
                   "write", abs_,
                   snapshot_path=snap_path, snapshot_kind=snap_kind,
                   original_uid=orig_uid, original_gid=orig_gid,
                   original_mode=orig_mode, size_bytes=len(content))
    await _emit_staged(ctx, seq, "write", abs_, size_bytes=len(content))
    state = "MOD" if pre_existing else "NEW"
    return f"Staged: write {abs_} ({len(content)} B, {state})"


async def edit_file(ctx, path: str, old_string: str, new_string: str) -> str:
    try:
        abs_ = await _resolve_and_gate_or_request(ctx, path, "write")
    except Exception as e:
        return _err(e)
    if not os.path.isfile(abs_):
        return f"Error: not a file: {abs_}"
    with open(abs_, "r", encoding="utf-8", errors="replace") as f:
        txt = f.read()
    n = txt.count(old_string)
    if n == 0:
        return "Error: old_string not found"
    if n > 1:
        return ("Error: old_string is not unique "
                f"({n} matches); add more surrounding context")
    new_txt = txt.replace(old_string, new_string, 1)
    return await write_file(ctx, abs_, new_txt)  # reuses staging path


async def delete_path(ctx, path: str, recursive: bool = False) -> str:
    try:
        abs_ = await _resolve_and_gate_or_request(ctx, path, "write")
    except Exception as e:
        return _err(e)
    if not os.path.exists(abs_):
        return f"Error: does not exist: {abs_}"
    # Refuse deleting visible_resources roots themselves
    roots = [r["path"] for r in ctx["conn"].execute(
        "SELECT path FROM visible_resources WHERE session_id=?",
        (ctx["session_id"],),
    )]
    if abs_ in roots:
        return "Error: refusing to delete visible_resources root"

    seq = _next_seq(ctx)
    if os.path.isfile(abs_):
        st = os.stat(abs_)
        try:
            snap = staging.maybe_take_file_snapshot(
                ctx["conn"], ctx["store"], ctx["session_id"],
                ctx["run_id"], str(seq), abs_)
        except SnapshotTooLarge as e:
            return _err(e)
        os.remove(abs_)
        staging.record(ctx["conn"], ctx["session_id"], ctx["run_id"], seq,
                       "delete_file", abs_,
                       snapshot_path=snap, snapshot_kind="file",
                       original_uid=st.st_uid, original_gid=st.st_gid,
                       original_mode=st.st_mode & 0o777,
                       size_bytes=st.st_size)
        await _emit_staged(ctx, seq, "delete_file", abs_,
                            size_bytes=st.st_size)
        return f"Staged: delete_file {abs_}"
    # directory branch
    is_empty = not any(os.scandir(abs_))
    if not is_empty and not recursive:
        return ("Error: directory is non-empty; "
                "pass recursive=True to delete it")
    snap_path = None
    snap_kind = None
    st = os.stat(abs_)
    if not is_empty:
        try:
            snap_path = ctx["store"].take_tar(
                ctx["session_id"], ctx["run_id"], str(seq), abs_)
            snap_kind = "tar"
        except SnapshotTooLarge as e:
            return _err(e)
    shutil.rmtree(abs_)
    staging.record(ctx["conn"], ctx["session_id"], ctx["run_id"], seq,
                   "delete_dir", abs_,
                   snapshot_path=snap_path, snapshot_kind=snap_kind,
                   original_uid=st.st_uid, original_gid=st.st_gid,
                   original_mode=st.st_mode & 0o777)
    await _emit_staged(ctx, seq, "delete_dir", abs_)
    return f"Staged: delete_dir {abs_}"


async def mkdir(ctx, path: str, parents: bool = False) -> str:
    try:
        abs_ = await _resolve_and_gate_or_request(ctx, path, "write")
    except Exception as e:
        return _err(e)
    if os.path.exists(abs_):
        return f"Error: already exists: {abs_}"
    seq = _next_seq(ctx)
    if parents:
        os.makedirs(abs_, exist_ok=False)
    else:
        os.mkdir(abs_)
    ownership.apply(abs_, ctx["chat_username"])
    staging.record(ctx["conn"], ctx["session_id"], ctx["run_id"], seq,
                   "mkdir", abs_)
    await _emit_staged(ctx, seq, "mkdir", abs_)
    return f"Staged: mkdir {abs_}"


async def rename(ctx, src: str, dst: str) -> str:
    try:
        s = await _resolve_and_gate_or_request(ctx, src, "write")
        d = await _resolve_and_gate_or_request(ctx, dst, "write")
    except Exception as e:
        return _err(e)
    if not os.path.exists(s):
        return f"Error: src does not exist: {s}"
    if os.path.exists(d):
        return ("Error: dst already exists; "
                "delete it first to keep undo history")
    seq = _next_seq(ctx)
    os.rename(s, d)
    staging.record(ctx["conn"], ctx["session_id"], ctx["run_id"], seq,
                   "rename", s, dst_path=d)
    await _emit_staged(ctx, seq, "rename", s, dst_path=d)
    return f"Staged: rename {s} -> {d}"


async def glob_files(ctx, pattern: str, root: str) -> str:
    try:
        abs_root = await _resolve_and_gate_or_request(ctx, root, "search")
    except Exception as e:
        return _err(e)
    visible_roots = [r["path"] for r in ctx["conn"].execute(
        "SELECT path FROM visible_resources WHERE session_id=? AND kind='folder'",
        (ctx["session_id"],),
    )]
    matches: list[str] = []
    truncated = False
    for dp, dirs, files in os.walk(abs_root):
        # Filter dirs in-place to avoid descending into ignored ones
        dirs[:] = [d for d in dirs
                   if not _is_blocked(os.path.join(dp, d), visible_roots,
                                       ctx.get("user_patterns", []))]
        for f in files:
            full = os.path.join(dp, f)
            if _is_blocked(full, visible_roots, ctx.get("user_patterns", [])):
                continue
            if fnmatch.fnmatch(f, pattern) or fnmatch.fnmatch(
                    os.path.relpath(full, abs_root), pattern):
                matches.append(full)
                if len(matches) >= GLOB_MAX_RESULTS:
                    truncated = True
                    break
        if truncated:
            break
    res = "\n".join(matches)
    if truncated:
        res += f"\n[truncated at {GLOB_MAX_RESULTS}]"
    return res


def _is_blocked(p: str, roots, user_patterns) -> bool:
    try:
        ignore.gate(p, roots, user_patterns)
        return False
    except (ignore.BlockedImplicit, ignore.BlockedHardBlacklist,
            ignore.BlockedGitignore):
        return True


async def search_content(ctx, query: str, root: str,
                          glob_pattern: Optional[str]) -> str:
    try:
        abs_root = await _resolve_and_gate_or_request(ctx, root, "search")
    except Exception as e:
        return _err(e)
    visible_roots = [r["path"] for r in ctx["conn"].execute(
        "SELECT path FROM visible_resources WHERE session_id=? AND kind='folder'",
        (ctx["session_id"],),
    )]
    pat = re.compile(query)
    out: list[str] = []
    for dp, dirs, files in os.walk(abs_root):
        dirs[:] = [d for d in dirs
                   if not _is_blocked(os.path.join(dp, d), visible_roots,
                                       ctx.get("user_patterns", []))]
        for f in files:
            full = os.path.join(dp, f)
            if _is_blocked(full, visible_roots, ctx.get("user_patterns", [])):
                continue
            if glob_pattern and not fnmatch.fnmatch(f, glob_pattern):
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    for i, line in enumerate(fh, 1):
                        if pat.search(line):
                            out.append(f"{full}:{i}:{line.rstrip()}")
                            if len(out) >= SEARCH_MAX_RESULTS:
                                return "\n".join(out) + (
                                    f"\n[truncated at {SEARCH_MAX_RESULTS}]")
            except OSError:
                continue
    return "\n".join(out) if out else "(no matches)"
