"""Pre-authorization rules for scheduled tasks — pure functions only.

A scheduled task can carry a `preauth` document describing what its unattended
run may do without a human on the other end of a confirmation card.  This
module owns two things and nothing else:

* :func:`parse` — normalize an untrusted document (JSON text or dict, typically
  straight out of ``scheduled_tasks.preauth_json``) into a fixed shape, silently
  dropping anything malformed.  It never raises: a broken rule must degrade to
  "no pre-authorization" (i.e. the normal confirmation gates), never to an
  error that aborts the run.
* :func:`shell_match` — decide whether a command literally matches one of the
  shell rules.

**This module deliberately knows nothing about safety.**  It answers "did the
author name this command?", not "is this command allowed?".  Every caller is
responsible for the gate: ``skills/shell.py::_run_allowlist_match`` additionally
requires a single simple command (no chaining, no redirection) and refuses
``protected``-level commands outright.  Keep the safety checks there — putting
them here would make this module untestable as a pure matcher and would let a
future caller believe a match implies permission.
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger("nimoos-agent")

# Only these rule kinds are honored for shell rules.
_SHELL_KINDS = ("prefix", "regex")

# The normalized document's keys; anything else in the input is dropped.
_STRING_LIST_FIELDS = ("egress_domains", "mcp_tools", "fs_write")

# ── Cost bounds (a preauth document is author-supplied data, not code) ────────
# Python's regex engine backtracks, and it runs on the agent's single event
# loop: one bad pattern stalls every session and every HTTP request with it.
# `^(a+)+$` against **30** a's blocks for ~29 s — the danger zone is tiny
# inputs, so an input-length cap alone is worthless (a 512-char cap does not
# even touch that PoC; measured 28.9 s with it in place). What actually works
# is refusing the pattern shape that causes exponential backtracking:
#   * _is_catastrophic_regex rejects a quantifier applied to a group that
#     itself contains a quantifier or an alternation — `(a+)+`, `(a*)*`,
#     `(a|a)+`, `(?:\s+)+` … — which is the classic ReDoS family;
#   * MAX_REGEX_COMMAND_LEN (128) bounds the polynomial cases the shape check
#     does not catch, and pre-authorized commands are short by nature;
#   * MAX_RULES caps how many patterns one command is tried against, so the
#     per-command cost cannot be multiplied by a long rule list.
# Detection is a heuristic, so it is applied in BOTH parse() and shell_match()
# and a rejected rule simply never matches (fail-closed for the grant, not for
# the run).
MAX_RULES = 64
MAX_REGEX_COMMAND_LEN = 128


_QUANTIFIER_CHARS = ("*", "+", "{")


def _is_catastrophic_regex(pattern: str) -> bool:
    """True if `pattern` has a nested quantifier — the ReDoS shape.

    Flags a quantified group (`(...)`+`*`/`+`/`{n,}`) whose body contains a
    quantifier or an alternation: `(a+)+`, `(a*)*`, `(a|a)+`, `(?:\\s+)+`.
    Those are the patterns whose match time is exponential in the input length.
    A group that is *not* quantified is fine (`^gh (pr|issue) list` stays
    usable), and so is a quantifier applied to a plain group (`(abc)+`).

    Heuristic, deliberately: it is a cost guard, not a decision procedure.
    Whatever it misses is still bounded by MAX_REGEX_COMMAND_LEN + MAX_RULES.
    """
    if not isinstance(pattern, str):
        return True
    stack: list[int] = []
    i, n = 0, len(pattern)
    in_class = False
    while i < n:
        ch = pattern[i]
        if ch == "\\":
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
            i += 1
            continue
        if ch == "[":
            in_class = True
        elif ch == "(":
            stack.append(i)
        elif ch == ")":
            start = stack.pop() if stack else None
            nxt = pattern[i + 1] if i + 1 < n else ""
            if start is not None and nxt in _QUANTIFIER_CHARS:
                body = pattern[start + 1:i]
                # Strip the group's own `(?...)` prefix so `(?:` / `(?i)` do not
                # count as content.
                if body.startswith("?"):
                    body = body[1:].lstrip(":=!<>PiLmsux")
                if _body_has_repetition(body):
                    return True
        i += 1
    return False


def _body_has_repetition(body: str) -> bool:
    """True if a group body contains an unescaped quantifier or alternation."""
    i, n = 0, len(body)
    in_class = False
    while i < n:
        ch = body[i]
        if ch == "\\":
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
            i += 1
            continue
        if ch == "[":
            in_class = True
        elif ch in ("*", "+", "|", "{"):
            return True
        i += 1
    return False


def _regex_is_usable(value: str) -> bool:
    """A regex rule is usable only if it compiles AND is not a ReDoS shape."""
    if _is_catastrophic_regex(value):
        logger.warning("preauth: rejecting regex rule with nested quantifier: %r",
                       value)
        return False
    try:
        re.compile(value)
    except re.error as exc:
        logger.warning("preauth: rejecting uncompilable regex rule %r: %s",
                       value, exc)
        return False
    return True


def parse(preauth_json) -> dict:
    """Normalize a preauth document.  Never raises; junk is dropped.

    Returns ``{"shell": [{"kind","value"}...], "egress_domains": [str],
    "mcp_tools": [str], "fs_write": [str]}`` — every key always present.
    Each list is truncated to :data:`MAX_RULES` entries (over-long lists are a
    cost problem, not a permission problem — the tail is dropped, not honored).

    Dropping is silent by design here; use :func:`parse_with_report` when the
    caller can tell the author what was thrown away (e.g. an API layer).
    """
    return parse_with_report(preauth_json)[0]


def parse_with_report(preauth_json) -> tuple[dict, dict]:
    """:func:`parse`, plus a report of what was dropped and why.

    The report is ``{"truncated": {field: {"kept": n, "dropped": n}},
    "rejected_rules": [{"field","value","reason"}]}`` — both empty when the
    document was accepted whole.  Rules are silently ignored at the gate, so a
    caller that talks to a human (the tasks API) should surface this; nothing
    in the run path depends on it.
    """
    raw = preauth_json
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}

    out: dict = {"shell": [], "egress_domains": [], "mcp_tools": [], "fs_write": []}
    report: dict = {"truncated": {}, "rejected_rules": []}

    shell_rules = raw.get("shell")
    if isinstance(shell_rules, (list, tuple)):
        for item in shell_rules:
            if not isinstance(item, dict):
                continue
            kind, value = item.get("kind"), item.get("value")
            if kind not in _SHELL_KINDS or not isinstance(value, str) or not value:
                continue
            if kind == "regex" and not _regex_is_usable(value):
                report["rejected_rules"].append({
                    "field": "shell", "value": value,
                    "reason": "unsafe_or_invalid_regex"})
                continue
            out["shell"].append({"kind": kind, "value": value})

    for field in _STRING_LIST_FIELDS:
        values = raw.get(field)
        # A bare string must NOT be iterated (it would decompose into chars).
        if not isinstance(values, (list, tuple)):
            continue
        out[field] = [v for v in values if isinstance(v, str) and v]

    for field, items in out.items():
        if len(items) > MAX_RULES:
            logger.warning(
                "preauth: %s has %d rules, truncating to %d",
                field, len(items), MAX_RULES)
            report["truncated"][field] = {"kept": MAX_RULES,
                                          "dropped": len(items) - MAX_RULES}
            out[field] = items[:MAX_RULES]

    return out, report


def shell_match(rules, command: str) -> bool:
    """True if `command` matches any rule.  Matching is start-anchored.

    ``prefix``: ``command.startswith(value)`` — a substring hit never counts.
    ``regex``: ``re.match`` (anchored at the start, like prefix); an unanchored
    pattern therefore cannot vouch for a command that merely *contains* it.
    A pattern that fails to compile — or that has a nested-quantifier (ReDoS)
    shape — is treated as "no match", never as an error.  Regex rules are also
    skipped for commands longer than :data:`MAX_REGEX_COMMAND_LEN`; prefix rules
    are linear and stay in play at any length.

    The shape check is repeated here (parse already applies it) because callers
    may hand over rule lists that never went through parse.
    """
    if not rules or not isinstance(command, str):
        return False
    regex_ok = len(command) <= MAX_REGEX_COMMAND_LEN
    for rule in list(rules)[:MAX_RULES]:
        if not isinstance(rule, dict):
            continue
        kind, value = rule.get("kind"), rule.get("value")
        if not isinstance(value, str) or not value:
            continue
        if kind == "prefix":
            if command.startswith(value):
                return True
        elif kind == "regex":
            if not regex_ok or _is_catastrophic_regex(value):
                continue
            try:
                if re.match(value, command) is not None:
                    return True
            except re.error:
                continue
    return False
