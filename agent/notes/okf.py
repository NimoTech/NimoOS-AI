"""OKF v0.1-compatible frontmatter (de)serialization for knowledge notes.

Tolerant by spec: unknown keys are preserved, unknown types are not
rejected, missing/broken frontmatter degrades to (meta={}, body=text).
Only `type` values are case-normalized (lowercase in memory/DB, Capitalized
on disk, matching OKF examples)."""
from __future__ import annotations

import yaml

NOTE_TYPES = ("note", "summary", "insight", "digest")

# OKF recommended keys first, NimoOS custom keys second, unknown keys last.
_KEY_ORDER = ("type", "title", "description", "tags", "timestamp",
              "id", "status", "created_by", "source_refs")

_DELIM = "---"


class _StrTimestampLoader(yaml.SafeLoader):
    """SafeLoader minus implicit timestamp resolution — frontmatter values
    like `timestamp: 2026-07-16T10:00:00+08:00` must stay strings (OKF/ISO
    8601 on disk; json-serializable in memory)."""


_StrTimestampLoader.yaml_implicit_resolvers = {
    key: [(tag, regexp) for tag, regexp in resolvers
          if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def parse_note_text(text: str) -> tuple[dict, str]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != _DELIM:
        return {}, text
    try:
        end = next(i for i in range(1, len(lines))
                   if lines[i].strip() == _DELIM)
    except StopIteration:
        return {}, text
    raw = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1:])
    try:
        meta = yaml.load(raw, Loader=_StrTimestampLoader)
    except yaml.YAMLError:
        return {}, text
    if not isinstance(meta, dict):
        return {}, text
    if isinstance(meta.get("type"), str):
        meta["type"] = meta["type"].strip().lower()
    return meta, body


def serialize_note_text(meta: dict, body: str) -> str:
    ordered: dict = {}
    for k in _KEY_ORDER:
        if k in meta and meta[k] not in (None, "", []):
            ordered[k] = meta[k]
    for k, v in meta.items():
        if k not in ordered and v not in (None, "", []):
            ordered[k] = v
    if isinstance(ordered.get("type"), str):
        ordered["type"] = ordered["type"].capitalize()
    fm = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True,
                        default_flow_style=None).rstrip("\n")
    if not body.endswith("\n"):
        body += "\n"
    return f"{_DELIM}\n{fm}\n{_DELIM}\n{body}"
