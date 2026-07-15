"""Deterministic shell command risk classifier.

Priority per segment: dangerous/protected > gray > safe. Across segments the
worst level wins. Unparseable input (parse.segments -> None) is GRAY, never SAFE.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from shell_guard.parse import Segment, extract_paths, segments

# ── SAFE read-only command names ──────────────────────────────────────────────
_SAFE_CMDS = {
    "ls", "tree", "cat", "head", "tail", "file", "grep", "egrep", "fgrep",
    "ps", "df", "du", "wc", "stat", "realpath", "basename", "dirname",
    "pwd", "which", "echo", "env", "printenv", "id", "uname", "hostname",
    "date", "cksum", "sha256sum", "md5sum",
}
# git subcommands that are read-only
_SAFE_GIT_SUB = {"status", "log", "diff", "show", "branch", "remote"}

# ── DANGEROUS verb/flag patterns ──────────────────────────────────────────────
_SHELL_CMDS = {"sh", "bash", "zsh", "dash"}
_DISK_CMDS = {"dd", "mkfs", "wipefs", "fdisk", "parted", "sgdisk", "shred"}
_PKG_SVC_CMDS = {"systemctl", "apt", "apt-get", "dpkg", "yum", "dnf"}


def _cmd_name(seg: Segment) -> str:
    return os.path.basename(seg.argv[0]) if seg.argv else ""


def _is_dangerous_seg(seg: Segment, all_segs: list[Segment], idx: int) -> str | None:
    name = _cmd_name(seg)
    flags = [a for a in seg.argv[1:] if a.startswith("-")]
    flagchars = "".join(f.lstrip("-") for f in flags)

    if name == "rm" and ("r" in flagchars or "f" in flagchars):
        return "recursive/forced remove"
    if name == "find" and "-delete" in seg.argv:
        return "find -delete"
    if name in _DISK_CMDS or name.startswith("mkfs"):
        return f"disk/format command: {name}"
    if name in ("chmod", "chown") and "R" in flagchars:
        return f"recursive {name}"
    if name in _PKG_SVC_CMDS:
        return f"package/service management: {name}"
    if name == "docker" and any(x in seg.argv for x in ("prune", "rm", "rmi")):
        return "docker destructive subcommand"
    # pipe-to-shell: this segment is a shell AND a previous segment fetches remotely
    if name in _SHELL_CMDS and idx > 0:
        prev = _cmd_name(all_segs[idx - 1])
        if prev in ("curl", "wget", "fetch"):
            return "pipe remote content to shell"
    # bash -c "$(...)" is already caught as unparseable by parse (returns gray),
    # but a literal 'bash -c curl...' still flags:
    if name in _SHELL_CMDS and "-c" in seg.argv:
        joined = " ".join(seg.argv)
        if re.search(r"\bcurl\b|\bwget\b", joined):
            return "shell -c fetching remote content"
    # fork bomb heuristic
    if ":(){" in "".join(seg.argv):
        return "fork bomb pattern"
    # redirect to a device node
    for tgt in seg.redirect_targets:
        if tgt.startswith("/dev/"):
            return f"redirect to device: {tgt}"
    return None


# ── PROTECTED path prefixes ───────────────────────────────────────────────────
_PROTECTED_PREFIXES = (
    "/etc", "/boot", "/usr", "/var/lib/nimoos", "/opt/nimoos",
)
_PROTECTED_SUFFIXES = (".key", ".pem")
_PROTECTED_SUBSTR = ("/.ssh/", "agent.db")
_DATA_ROOT = "/DATA"


def _resolve(p: str) -> str:
    try:
        return os.path.realpath(p)
    except OSError:
        return p


def _is_protected_path(raw: str) -> bool:
    rp = _resolve(raw)
    if any(rp == pre or rp.startswith(pre + "/") for pre in _PROTECTED_PREFIXES):
        return True
    if rp.endswith(_PROTECTED_SUFFIXES):
        return True
    if any(s in rp for s in _PROTECTED_SUBSTR):
        return True
    return False


def _is_data_mass(raw: str) -> bool:
    # /DATA top-level or wildcard delete: /DATA, /DATA/, /DATA/*
    rp = raw.rstrip("/")
    if raw.endswith("*") and rp.rstrip("*").rstrip("/") in (_DATA_ROOT, ""):
        return True
    return _resolve(rp) == _DATA_ROOT


# ── Result ────────────────────────────────────────────────────────────────────
@dataclass
class Decision:
    level: str  # "safe" | "gray" | "dangerous" | "protected"
    reason: str = ""
    paths: list[str] = field(default_factory=list)


_RANK = {"safe": 0, "gray": 1, "dangerous": 2, "protected": 3}


def _worse(a: Decision, b: Decision) -> Decision:
    return a if _RANK[a.level] >= _RANK[b.level] else b


def _classify_seg(seg: Segment, all_segs: list[Segment], idx: int) -> Decision:
    if not seg.argv:
        # pure redirection segment; treat targets for protection
        pass
    name = _cmd_name(seg)

    # protected paths (checked regardless of verb)
    hit_paths = [p for p in extract_paths(seg)
                 if _is_protected_path(p) or _is_data_mass(p)]
    if hit_paths:
        return Decision("protected", f"touches protected path(s): {hit_paths}", hit_paths)

    danger = _is_dangerous_seg(seg, all_segs, idx)
    if danger:
        return Decision("dangerous", danger, extract_paths(seg))

    has_write_redirect = bool(seg.redirect_targets)
    if name in _SAFE_CMDS and not has_write_redirect:
        return Decision("safe")
    if name == "git" and len(seg.argv) > 1 and seg.argv[1] in _SAFE_GIT_SUB \
            and not has_write_redirect:
        return Decision("safe")

    # everything else that isn't clearly safe or clearly dangerous
    return Decision("gray", f"unclassified command: {name or '(redirect)'}")


def classify(command: str) -> Decision:
    segs = segments(command)
    if segs is None:
        return Decision("gray", "command could not be parsed (obfuscation/substitution)")
    if not segs:
        return Decision("safe")
    result = Decision("safe")
    for idx, seg in enumerate(segs):
        result = _worse(result, _classify_seg(seg, segs, idx))
    return result
