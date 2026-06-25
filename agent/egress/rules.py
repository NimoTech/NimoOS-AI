"""
egress/rules.py — Privacy rules: path blacklist + content regex DLP.

assess(files, inline_payload) -> Verdict

Verdict levels (in priority order):
  "block"   — high-confidence sensitive data; refuse upload
  "suspect" — medium-confidence; surface for human review
  "clean"   — no known-sensitive content detected

Pure stdlib + pathspec (already a project dep via fs.ignore). Python 3.11+.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from fs.ignore import BUILTIN_HARD_BLACKLIST, _matches  # noqa: WPS450
from pathspec import PathSpec

# ─── Config ──────────────────────────────────────────────────────────────────

_DEFAULT_MAXBYTES = 4096
_HARD_SPEC = PathSpec.from_lines("gitignore", BUILTIN_HARD_BLACKLIST)


def _maxbytes() -> int:
    try:
        return int(os.environ.get("NIMOOS_EGRESS_JUDGE_MAXBYTES", _DEFAULT_MAXBYTES))
    except (ValueError, TypeError):
        return _DEFAULT_MAXBYTES


# ─── Result type ─────────────────────────────────────────────────────────────

@dataclass
class Verdict:
    level: str   # "block" | "suspect" | "clean"
    reason: str


# ─── Content patterns ────────────────────────────────────────────────────────

# High-danger (→ block)
_RE_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    re.ASCII,
)
_RE_AWS_AKID = re.compile(
    r"AKIA[0-9A-Z]{16}",
    re.ASCII,
)

# Medium-danger (→ suspect)
_RE_JWT = re.compile(
    r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
    re.ASCII,
)
_RE_EMAIL = re.compile(r"\S+@\S+\.\S+")
_RE_CN_PHONE = re.compile(r"1[3-9]\d{9}")
_RE_CN_ID = re.compile(r"\d{17}[\dXx]")

# PII density threshold: total matches of (email + phone + id card) across content
_PII_DENSITY_THRESHOLD = 3


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _path_blocked(path: str) -> bool:
    """Return True if the file path hits the built-in hard blacklist."""
    return _matches(_HARD_SPEC, path)


def _read_head(path: str, maxbytes: int) -> bytes | None:
    """
    Read up to *maxbytes* from the start of *path*.

    Returns None on any error (missing file, permission denied, etc.).
    Binary detection is left to callers: even binary blobs may contain
    embedded plaintext secrets, so we attempt the read regardless.
    """
    try:
        with open(path, "rb") as fh:
            return fh.read(maxbytes)
    except OSError:
        return None


def _scan_content(data: bytes) -> Verdict | None:
    """
    Scan raw bytes for known sensitive patterns.

    Returns a Verdict if a pattern fires, or None if clean.
    Priority: block patterns first, then suspect.
    """
    # Attempt UTF-8 decode; replace undecodable bytes with replacement char
    # so regex patterns can still match ASCII sequences embedded in binary.
    text = data.decode("utf-8", errors="replace")

    # High-danger → block
    if _RE_PRIVATE_KEY.search(text):
        return Verdict(level="block", reason="private key header detected in content")
    if _RE_AWS_AKID.search(text):
        return Verdict(level="block", reason="AWS access key ID (AKIA…) detected in content")

    # Medium-danger → suspect
    if _RE_JWT.search(text):
        return Verdict(level="suspect", reason="JWT token detected in content")

    pii_count = (
        len(_RE_EMAIL.findall(text))
        + len(_RE_CN_PHONE.findall(text))
        + len(_RE_CN_ID.findall(text))
    )
    if pii_count >= _PII_DENSITY_THRESHOLD:
        return Verdict(
            level="suspect",
            reason=f"high PII density ({pii_count} matches: email/phone/ID) in content",
        )

    return None


# ─── Verdict level ordering ──────────────────────────────────────────────────

_LEVEL_RANK = {"clean": 0, "suspect": 1, "block": 2}


def _worse(a: Verdict | None, b: Verdict | None) -> Verdict | None:
    """Return whichever verdict has the higher (more dangerous) level."""
    if a is None:
        return b
    if b is None:
        return a
    return a if _LEVEL_RANK[a.level] >= _LEVEL_RANK[b.level] else b


# ─── Public API ───────────────────────────────────────────────────────────────

def assess(
    files: list[str],
    inline_payload: bytes | None = None,
) -> Verdict:
    """
    Assess whether the upload is safe to proceed.

    All files and inline_payload are evaluated; the highest-severity verdict
    wins (block > suspect > clean).  This ensures a later file containing a
    private key is not hidden by an earlier file that only triggered 'suspect'.

    Evaluation stages:
      1. Path blacklist check on every file path (no I/O) — block fires immediately;
         a single blacklisted path returns block without reading any content.
      2. Content scan of each file's first NIMOOS_EGRESS_JUDGE_MAXBYTES bytes.
         Read failure → suspect for that file; scanning continues for remaining files.
      3. Content scan of inline_payload (if provided).
      4. Worst verdict seen across all stages is returned; if none fired → clean.

    Args:
        files: Absolute (or relative) paths of files being uploaded.
        inline_payload: Raw bytes of inline data (stdin / in-command body).

    Returns:
        Verdict with level in {"block", "suspect", "clean"} and a reason string.
    """
    maxbytes = _maxbytes()

    # ── Stage 1: path blacklist (no file I/O needed) ─────────────────────────
    # Fail-fast: a single blacklisted path is enough to block unconditionally.
    for path in files:
        if _path_blocked(path):
            return Verdict(
                level="block",
                reason=f"path matches hard blacklist: {path}",
            )

    # ── Stage 2: content scan for each named file ────────────────────────────
    # Accumulate the worst verdict; do NOT short-circuit on 'suspect' so that
    # a later file containing a private key (block) is not hidden.
    worst: Verdict | None = None

    for path in files:
        data = _read_head(path, maxbytes)
        if data is None:
            # Unreadable → conservative: treat as suspect and keep scanning.
            candidate = Verdict(
                level="suspect",
                reason=f"could not read file for content scan: {path}",
            )
        else:
            candidate = _scan_content(data)

        worst = _worse(worst, candidate)
        # Short-circuit if we already know we will block.
        if worst is not None and worst.level == "block":
            return worst

    # ── Stage 3: inline payload scan ─────────────────────────────────────────
    if inline_payload is not None:
        chunk = inline_payload[:maxbytes]
        candidate = _scan_content(chunk)
        worst = _worse(worst, candidate)

    if worst is not None:
        return worst

    return Verdict(level="clean", reason="no sensitive patterns detected")
