"""Adaptive pre-execution backstop for destructive shell commands.

btrfs volume  -> read-only subvolume snapshot (covers everything).
ext4/xfs      -> recursive hardlink (cp -al) of parseable targets into a
                 rolling trash dir (near-zero cost; survives the delete).
unparseable   -> BackstopResult(kind="none", undoable=False) — caller warns.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass

logger = logging.getLogger("nimoos-agent")

_DEFAULT_TRASH_ROOT = "/var/lib/nimoos/ai/agent/shell-trash"


@dataclass
class BackstopResult:
    kind: str          # "snapshot" | "trash" | "none"
    location: str = ""
    undoable: bool = False
    note: str = ""


def fs_type(path: str) -> str:
    probe = path
    while probe and not os.path.exists(probe):
        probe = os.path.dirname(probe)
    if not probe:
        return "unknown"
    try:
        out = subprocess.run(
            ["stat", "-f", "-c", "%T", probe],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or "unknown"
    except (subprocess.SubprocessError, OSError):
        return "unknown"


def _snapshot_btrfs(target: str, trash_root: str) -> BackstopResult | None:
    stamp = str(int(time.time() * 1000))
    dest = os.path.join(trash_root, f"snap-{stamp}")
    os.makedirs(trash_root, exist_ok=True)
    # Snapshot the enclosing mountpoint's subvolume; best-effort.
    try:
        subprocess.run(
            ["btrfs", "subvolume", "snapshot", "-r", target, dest],
            capture_output=True, check=True, timeout=30,
        )
        return BackstopResult("snapshot", dest, True, "btrfs read-only snapshot")
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("backstop: btrfs snapshot failed for %s: %s", target, exc)
        return None


def _hardlink_trash(paths: list[str], trash_root: str) -> BackstopResult:
    stamp = str(int(time.time() * 1000))
    dest_dir = os.path.join(trash_root, stamp)
    os.makedirs(dest_dir, exist_ok=True)
    saved = 0
    for p in paths:
        if not os.path.exists(p):
            continue
        base = os.path.basename(p.rstrip("/")) or "root"
        dest = os.path.join(dest_dir, base)
        try:
            # cp -al: recursive hardlink; instant, no data copy, same-fs only.
            subprocess.run(["cp", "-al", p, dest],
                           capture_output=True, check=True, timeout=60)
            saved += 1
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("backstop: hardlink failed for %s: %s", p, exc)
    if saved:
        return BackstopResult("trash", dest_dir, True,
                              f"hardlinked {saved} path(s) to rolling trash")
    return BackstopResult("none", "", False, "no target could be backed up")


def prepare_backstop(paths: list[str], trash_root: str | None = None) -> BackstopResult:
    root = trash_root or _DEFAULT_TRASH_ROOT
    real = [p for p in paths if p and os.path.exists(p)]
    if not real:
        return BackstopResult("none", "", False, "no existing target to back up")

    # btrfs branch: if any target is on btrfs, snapshot its volume.
    for p in real:
        if fs_type(p) == "btrfs":
            snap = _snapshot_btrfs(p, root)
            if snap is not None:
                return snap
            break  # snapshot failed → fall through to hardlink

    return _hardlink_trash(real, root)


def prune(trash_root: str, keep: int) -> int:
    if not os.path.isdir(trash_root):
        return 0
    entries = sorted(
        (e for e in os.scandir(trash_root) if e.is_dir()),
        key=lambda e: e.name,
    )
    to_remove = entries[:-keep] if keep > 0 else entries
    removed = 0
    import shutil
    for e in to_remove:
        shutil.rmtree(e.path, ignore_errors=True)
        removed += 1
    return removed
