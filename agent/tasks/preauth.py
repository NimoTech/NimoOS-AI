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
requires a single simple command (no chaining, no redirection), refuses
``protected``-level commands, and refuses interpreters.  Keep the safety checks
there — putting them here would make this module untestable as a pure matcher
and would let a future caller believe a match implies permission.

**Shell rules are PREFIX ONLY.**  `kind: "regex"` was supported in the first two
drafts and has been removed: Python's regex engine backtracks and runs on the
agent's single event loop, so one pattern in a stored document could stall every
session and every HTTP request.  Two rounds of static ReDoS detection were tried
and both leaked — the second missed `^a*a*a*a*a*a*a*a*a*a*$` (4.95 s),
`^(a?){30}a{30}$` (31 s) and `^\\s*\\s*…$` (7 s), which a 64-rule document
multiplies into minutes of dead event loop.  A thread pool is not a fix either
(Python cannot kill a running thread; the CPU burns on).  Real rules are prefixes
anyway — `lark-cli `, `gh pr list`, `date` — and Task 7's from-denied generator
emits prefixes, so the expressiveness lost is theoretical and the cost avoided
is not.  Documents that still carry regex rules are accepted, but those rules
are dropped and reported (never an error).
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("nimoos-agent")

# The only honored shell rule kind. See the module docstring for why `regex`
# is gone; `_REJECTED_SHELL_KINDS` exists so old documents get a clear report
# rather than a silent drop.
_SHELL_KINDS = ("prefix",)
_REJECTED_SHELL_KINDS = ("regex",)

# The normalized document's keys; anything else in the input is dropped.
_STRING_LIST_FIELDS = ("egress_domains", "mcp_tools", "fs_write")

# A preauth document is author-supplied data. Prefix matching is linear in the
# command length, so the only cost left to bound is the rule count: one command
# must not be matched against an unbounded list.
MAX_RULES = 64


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
            if not isinstance(value, str) or not value:
                continue
            if kind in _REJECTED_SHELL_KINDS:
                logger.warning(
                    "preauth: dropping unsupported %s shell rule %r "
                    "(prefix rules only)", kind, value)
                report["rejected_rules"].append({
                    "field": "shell", "value": value,
                    "reason": "regex_rules_not_supported"})
                continue
            if kind not in _SHELL_KINDS:
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
    """True if `command` starts with one of the rules' prefixes.

    ``command.startswith(value)`` — start-anchored, so a substring hit never
    counts, and the command is NOT stripped first (leading whitespace is part of
    what the author would have had to authorize).  Any non-``prefix`` rule
    (notably a leftover ``regex`` rule from an older document) is ignored, which
    is why no pattern from a stored document can ever be compiled or executed.
    At most :data:`MAX_RULES` rules are consulted.
    """
    if not rules or not isinstance(command, str):
        return False
    for rule in list(rules)[:MAX_RULES]:
        if not isinstance(rule, dict):
            continue
        if rule.get("kind") != "prefix":
            continue
        value = rule.get("value")
        if not isinstance(value, str) or not value:
            continue
        if command.startswith(value):
            return True
    return False
