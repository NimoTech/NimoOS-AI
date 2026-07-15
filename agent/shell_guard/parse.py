"""Best-effort bash command decomposition for risk classification.

We do NOT aim to fully emulate bash. We tokenize (respecting quotes and shell
operators) and split into segments on control operators. Anything we cannot
statically tokenize returns None so the caller treats it as GRAY (never SAFE).
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field

# The complete set of bash control operators that separate commands. Keep this
# complete — a missing operator lets a compound command collapse into one
# segment and slip past single-segment guards (allowlist match / upload deferral).
_OPERATORS = {"|", "||", "&&", ";", "&", "|&", ";;", ";&", ";;&"}
_REDIRECT_OPS = {">", ">>", "&>"}     # write
_READ_OPS = {"<", "<<"}               # read
_ALL_REDIRECT = {">", ">>", "&>", "<", "<<"}


@dataclass
class Segment:
    argv: list[str] = field(default_factory=list)
    redirect_targets: list[str] = field(default_factory=list)   # write
    read_targets: list[str] = field(default_factory=list)       # read


def _split_unquoted_newlines(command: str) -> str:
    """Bash treats an unquoted newline as a command separator. shlex would
    swallow it as whitespace, collapsing multiple commands into one segment,
    so normalize unquoted newlines/CRs to ';'. Newlines inside quotes are
    preserved verbatim."""
    out = []
    quote = None
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if quote is not None:
            out.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                out.append(command[i + 1]); i += 2; continue
            if c == quote:
                quote = None
        elif c in ("'", '"'):
            quote = c; out.append(c)
        elif c in ("\n", "\r"):
            out.append(";")
            # Consume the rest of a newline/CR run so `\r\n` or blank lines do
            # not emit `;;` — shlex would group consecutive punctuation into a
            # single non-operator token, collapsing the split.
            while i + 1 < n and command[i + 1] in ("\n", "\r"):
                i += 1
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _tokenize(command: str) -> list[str] | None:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None  # unbalanced quotes / bad escape → unparseable


def segments(command: str) -> list[Segment] | None:
    command = _split_unquoted_newlines(command)
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
