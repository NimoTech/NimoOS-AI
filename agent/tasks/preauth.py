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
import re

# Only these rule kinds are honored for shell rules.
_SHELL_KINDS = ("prefix", "regex")

# The normalized document's keys; anything else in the input is dropped.
_STRING_LIST_FIELDS = ("egress_domains", "mcp_tools", "fs_write")


def parse(preauth_json) -> dict:
    """Normalize a preauth document.  Never raises; junk is dropped.

    Returns ``{"shell": [{"kind","value"}...], "egress_domains": [str],
    "mcp_tools": [str], "fs_write": [str]}`` — every key always present.
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

    shell_rules = raw.get("shell")
    if isinstance(shell_rules, (list, tuple)):
        for item in shell_rules:
            if not isinstance(item, dict):
                continue
            kind, value = item.get("kind"), item.get("value")
            if kind in _SHELL_KINDS and isinstance(value, str) and value:
                out["shell"].append({"kind": kind, "value": value})

    for field in _STRING_LIST_FIELDS:
        values = raw.get(field)
        # A bare string must NOT be iterated (it would decompose into chars).
        if not isinstance(values, (list, tuple)):
            continue
        out[field] = [v for v in values if isinstance(v, str) and v]

    return out


def shell_match(rules, command: str) -> bool:
    """True if `command` matches any rule.  Matching is start-anchored.

    ``prefix``: ``command.startswith(value)`` — a substring hit never counts.
    ``regex``: ``re.match`` (anchored at the start, like prefix); an unanchored
    pattern therefore cannot vouch for a command that merely *contains* it.
    A pattern that fails to compile is treated as "no match", never as an error.
    """
    if not rules or not isinstance(command, str):
        return False
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        kind, value = rule.get("kind"), rule.get("value")
        if not isinstance(value, str) or not value:
            continue
        if kind == "prefix":
            if command.startswith(value):
                return True
        elif kind == "regex":
            try:
                if re.match(value, command) is not None:
                    return True
            except re.error:
                continue
    return False
