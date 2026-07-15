"""Best-effort bash command decomposition for risk classification.

We do NOT aim to fully emulate bash. We tokenize (respecting quotes and shell
operators) and split into segments on control operators. Anything we cannot
statically tokenize returns None so the caller treats it as GRAY (never SAFE).
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field

_OPERATORS = {"|", "||", "&&", ";", "&"}
_REDIRECT_OPS = {">", ">>", "&>"}     # write
_READ_OPS = {"<", "<<"}               # read
_ALL_REDIRECT = {">", ">>", "&>", "<", "<<"}


@dataclass
class Segment:
    argv: list[str] = field(default_factory=list)
    redirect_targets: list[str] = field(default_factory=list)   # write
    read_targets: list[str] = field(default_factory=list)       # read


def _tokenize(command: str) -> list[str] | None:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None  # unbalanced quotes / bad escape → unparseable


def segments(command: str) -> list[Segment] | None:
    toks = _tokenize(command)
    if toks is None:
        return None
    # Subshell / command-substitution markers we won't statically resolve:
    # surface conservatively as unparseable so the classifier sends to GRAY.
    if any(ch in command for ch in ("$(", "`")):
        return None

    segs: list[Segment] = []
    cur = Segment()
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in _OPERATORS:
            if cur.argv or cur.redirect_targets or cur.read_targets:
                segs.append(cur)
            cur = Segment()
            i += 1
            continue
        if t in _ALL_REDIRECT:
            # next token is the redirect target
            if t in _REDIRECT_OPS and i + 1 < len(toks):
                cur.redirect_targets.append(toks[i + 1])
            elif t in _READ_OPS and i + 1 < len(toks):
                cur.read_targets.append(toks[i + 1])
            i += 2
            continue
        cur.argv.append(t)
        i += 1
    if cur.argv or cur.redirect_targets or cur.read_targets:
        segs.append(cur)
    return segs


def extract_paths(seg: Segment) -> list[str]:
    paths: list[str] = []
    for tok in seg.argv[1:]:  # skip the command name itself
        if tok.startswith("-"):
            continue
        if tok.startswith("/") or "/" in tok:
            paths.append(tok)
    paths.extend(seg.redirect_targets)
    paths.extend(seg.read_targets)
    return paths
