"""Batch structural-fs orchestration: preflight -> (grant) -> re-preflight ->
commit. Preflight is pure (no disk writes, no prompts)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from fs import validators
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
