"""Three-layer ignore matcher.

Order (top wins):
  1. IMPLICIT_IGNORE  — never visible; cannot be force-overridden
  2. BUILTIN_HARD_BLACKLIST + user_patterns — never visible; cannot be force-overridden
  3. .gitignore (recursive merge) — can be force-overridden by picker @-mention
"""
from __future__ import annotations

import os
from pathlib import PurePosixPath

from pathspec import PathSpec


class BlockedImplicit(Exception):
    pass


class BlockedHardBlacklist(Exception):
    pass


class BlockedGitignore(Exception):
    pass


IMPLICIT_IGNORE = [
    ".git/", ".hg/", ".svn/",
    ".nimoos-agent/", ".nimoos-agent-snapshots/",
    "agent.db", "agent.db-wal", "agent.db-shm",
    "*.swp", ".DS_Store", "Thumbs.db",
]

BUILTIN_HARD_BLACKLIST = [
    # credentials / keys
    ".ssh/", ".gnupg/", ".pki/", ".aws/",
    ".config/gcloud/", ".docker/config.json",
    "*.key", "*.pem", "*.p12", "*.pfx",
    "id_rsa*", "id_ed25519*", "id_ecdsa*",
    # absolute system paths (matched after stripping leading slash for spec lib)
    "etc/", "root/", "proc/", "sys/", "dev/", "boot/",
    "usr/", "bin/", "sbin/", "lib/", "lib64/",
    "var/lib/nimoos/", "usr/share/nimoos/", "opt/nimoos/",
]

_IMPLICIT_SPEC = PathSpec.from_lines("gitignore", IMPLICIT_IGNORE)
_HARD_SPEC = PathSpec.from_lines("gitignore", BUILTIN_HARD_BLACKLIST)


def _matches(spec: PathSpec, abs_path: str) -> bool:
    # gitwildmatch wants relative-style paths. Strip leading slash so absolute
    # patterns above ("etc/", "root/") catch matches.
    rel = abs_path.lstrip("/")
    if spec.match_file(rel):
        return True
    # also try the basename so patterns like "*.key" anywhere catch
    return spec.match_file(os.path.basename(abs_path))


def _matches_user(user_patterns: list[str], abs_path: str) -> bool:
    if not user_patterns:
        return False
    spec = PathSpec.from_lines("gitignore", user_patterns)
    return _matches(spec, abs_path)


def _gitignore_blocks(abs_path: str, visible_roots: list[str]) -> bool:
    """Walk visible roots; if abs_path is under one, merge .gitignore from
    each level between root and the file (inclusive at the file's parent)."""
    for root in visible_roots:
        root_abs = os.path.abspath(root).rstrip(os.sep)
        if not (abs_path == root_abs or abs_path.startswith(root_abs + os.sep)):
            continue
        rel = os.path.relpath(abs_path, root_abs)
        if rel.startswith(".."):
            continue
        parts = PurePosixPath(rel).parts
        # Build progressive list of dirs to scan: root, root/parts[0],
        # ..., root/parts[0]/.../parts[-2]. (Don't read the file itself.)
        dirs_to_scan = [root_abs]
        for i in range(len(parts) - 1):
            dirs_to_scan.append(os.path.join(root_abs, *parts[: i + 1]))
        merged_lines: list[str] = []
        for d in dirs_to_scan:
            gi = os.path.join(d, ".gitignore")
            if os.path.isfile(gi):
                try:
                    with open(gi, "r", encoding="utf-8", errors="replace") as f:
                        merged_lines.extend(f.read().splitlines())
                except OSError:
                    pass
        if not merged_lines:
            return False
        spec = PathSpec.from_lines("gitignore", merged_lines)
        return spec.match_file(rel)
    return False


def gate(abs_path: str,
         visible_roots: list[str],
         user_patterns: list[str],
         allow_gitignore_override: bool = False) -> None:
    """Raise the most specific Blocked* exception for the first layer that
    blocks abs_path. Returns None if all layers pass."""
    if _matches(_IMPLICIT_SPEC, abs_path):
        raise BlockedImplicit(f"implicit ignore: {abs_path}")
    if _matches(_HARD_SPEC, abs_path):
        raise BlockedHardBlacklist(f"built-in hard blacklist: {abs_path}")
    if _matches_user(user_patterns, abs_path):
        raise BlockedHardBlacklist(f"user hard blacklist: {abs_path}")
    if not allow_gitignore_override and _gitignore_blocks(abs_path, visible_roots):
        raise BlockedGitignore(f".gitignore: {abs_path}")
