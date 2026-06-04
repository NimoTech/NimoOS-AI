"""Batch structural-fs orchestration: preflight -> (grant) -> re-preflight ->
commit. Preflight is pure (no disk writes, no prompts)."""
from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass, field

from fs import staging, validators
from fs.vtree import VTree, VTreeError

MAX_OPS = 500
MAX_DELETE_ENTRIES = 2000      # safety valve: files under a recursive delete
MAX_DELETE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB


@dataclass
class PreflightResult:
    ok: list = field(default_factory=list)
    need_grant: list = field(default_factory=list)
    blocked: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def _estimate_dir(path: str) -> tuple[int, int]:
    """Shallow-recursive count of (entries, bytes) under a dir, bailing early
    once MAX is exceeded. Read-only."""
    count = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for f in filenames:
            count += 1
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
            if count > MAX_DELETE_ENTRIES or total > MAX_DELETE_BYTES:
                return count, total
    return count, total


def preflight(ctx, operations: list) -> PreflightResult:
    res = PreflightResult()
    if len(operations) > MAX_OPS:
        res.errors.append({"index": -1, "op": "batch",
                           "reason": f"操作数 {len(operations)} 超过上限 {MAX_OPS}"})
        return res

    vt = VTree()
    grant_seen = set()

    for i, op in enumerate(operations):
        kind = op["op"]
        # ---- resolve + gate every path this op touches ----
        raw_paths = [op["path"]] + ([op["dst"]] if op.get("dst") else [])
        abs_paths = []
        fatal = False
        for raw in raw_paths:
            cat, ap = validators.classify(ctx, raw)
            if cat == "blocked":
                res.blocked.append({"path": ap, "reason": "受保护路径,已忽略"})
                fatal = True
            elif cat == "need_grant":
                if ap not in grant_seen:
                    grant_seen.add(ap)
                    res.need_grant.append(ap)
                abs_paths.append(ap)
            else:
                abs_paths.append(ap)
        if fatal:
            continue  # blocked op: do not apply to vtree

        # ---- safety valve for recursive delete ----
        if kind == "delete" and op.get("recursive") and os.path.isdir(abs_paths[0]):
            cnt, byt = _estimate_dir(abs_paths[0])
            if cnt > MAX_DELETE_ENTRIES or byt > MAX_DELETE_BYTES:
                res.errors.append({"index": i, "op": kind,
                                   "reason": f"目录过大(约 {cnt} 项),请手动或用终端删除"})
                continue

        # ---- virtual-tree validation (cascade / circular / empty) ----
        try:
            if kind == "mkdir":
                vt.mkdir(abs_paths[0], parents=op.get("parents", False))
            elif kind == "rename":
                vt.rename(abs_paths[0], abs_paths[1])
            elif kind == "delete":
                vt.delete(abs_paths[0], recursive=op.get("recursive", False))
            else:
                res.errors.append({"index": i, "op": kind,
                                   "reason": f"未知操作: {kind}"})
                continue
        except VTreeError as e:
            res.errors.append({"index": i, "op": kind, "reason": str(e)})
            continue

        res.ok.append({**op, "_abs": abs_paths})

    return res


def _next_seq(ctx) -> int:
    row = ctx["conn"].execute(
        "SELECT COALESCE(MAX(seq), 0) AS m FROM staged_changes "
        "WHERE session_id=? AND run_id=?",
        (ctx["session_id"], ctx["run_id"]),
    ).fetchone()
    return (row["m"] or 0) + 1


async def commit(ctx, result) -> str:
    """Apply result.ok in order. Each op re-checks existence at the last moment
    (defense-in-depth vs preflight->commit TOCTOU) before mutating disk. Tags
    every staged_changes row with a shared batch_id and emits one staged_batch
    event. Raises on first OS-level failure (rows already staged keep batch_id)."""
    conn = ctx["conn"]
    batch_id = uuid.uuid4().hex
    items = []
    summary = {"mkdir": 0, "rename": 0, "delete": 0}

    for entry in result.ok:
        kind = entry["op"]
        abs_paths = entry["_abs"]
        seq = _next_seq(ctx)
        if kind == "mkdir":
            target = abs_paths[0]
            if os.path.exists(target):           # last-moment guard
                continue
            if entry.get("parents"):
                os.makedirs(target, exist_ok=False)
            else:
                os.mkdir(target)
            staging.record(conn, ctx["session_id"], ctx["run_id"], seq,
                           "mkdir", target, batch_id=batch_id)
            summary["mkdir"] += 1
            items.append({"seq": seq, "op": "mkdir", "path": target})
        elif kind == "rename":
            src, dst = abs_paths[0], abs_paths[1]
            if not os.path.exists(src) or os.path.exists(dst):  # last-moment guard
                raise RuntimeError(f"rename precondition changed: {src} -> {dst}")
            os.rename(src, dst)
            staging.record(conn, ctx["session_id"], ctx["run_id"], seq,
                           "rename", src, dst_path=dst, batch_id=batch_id)
            summary["rename"] += 1
            items.append({"seq": seq, "op": "rename", "path": src, "dst_path": dst})
        elif kind == "delete":
            target = abs_paths[0]
            if not os.path.exists(target):       # last-moment guard
                continue
            st = os.stat(target)
            if os.path.isfile(target):
                snap = staging.maybe_take_file_snapshot(
                    conn, ctx["store"], ctx["session_id"], ctx["run_id"],
                    str(seq), target)
                os.remove(target)
                staging.record(conn, ctx["session_id"], ctx["run_id"], seq,
                               "delete_file", target,
                               snapshot_path=snap, snapshot_kind="file",
                               original_uid=st.st_uid, original_gid=st.st_gid,
                               original_mode=st.st_mode & 0o777,
                               size_bytes=st.st_size, batch_id=batch_id)
            else:
                is_empty = not any(os.scandir(target))
                snap = None
                kindsnap = None
                if not is_empty:
                    snap = ctx["store"].take_tar(ctx["session_id"], ctx["run_id"],
                                                 str(seq), target)
                    kindsnap = "tar"
                shutil.rmtree(target)
                staging.record(conn, ctx["session_id"], ctx["run_id"], seq,
                               "delete_dir", target,
                               snapshot_path=snap, snapshot_kind=kindsnap,
                               original_uid=st.st_uid, original_gid=st.st_gid,
                               original_mode=st.st_mode & 0o777,
                               batch_id=batch_id)
            summary["delete"] += 1
            items.append({"seq": seq, "op": "delete", "path": target})

    await ctx["sink"].put({
        "type": "staged_batch", "run_id": ctx["run_id"], "batch_id": batch_id,
        "summary": summary, "items": items,
    })
    return batch_id
