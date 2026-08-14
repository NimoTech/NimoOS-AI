"""Canonical tool fingerprints: approval invalidation gate (schema_hash) and "description changed" badge (desc_hash).

This is deliberately the only implementation of these hashes in the system.
The Go side is responsible for storage only, not recomputation. If both sides
have normalization logic that differs by even a hair (key order, separators,
non-ASCII escaping), it will silently void every approval the user ever granted.
"""
from __future__ import annotations

import hashlib
import json

_HASH_LEN = 16


def _hash(obj) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_HASH_LEN]


def schema_hash(input_schema) -> str:
    """Fingerprint of the tool's input_schema. Participates in approval invalidation
    (design doc § 5.2 interface gate).

    None and {} converge: it is legal for the server to not provide inputSchema;
    if their hashes differ, each probe would determine "interface changed"
    and re-ask the user.
    """
    return _hash(input_schema if input_schema is not None else {})


def desc_hash(description) -> str:
    """Fingerprint of the tool's description. Does NOT participate in any gate
    (design doc § 5.2.1) — it only drives the "description changed" badge
    in the settings UI. Folding description into schema_hash would cause
    the server to re-interrogate the user about every tool whenever it
    fixes a typo, rewords it, or adds localization.
    """
    return _hash(description or "")
