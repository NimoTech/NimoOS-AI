from notes.okf import parse_note_text, serialize_note_text


SAMPLE = """---
type: Insight
title: t1
description: d1
tags: [a, b]
timestamp: 2026-07-16T10:00:00+08:00
id: 018f-xyz
status: draft
created_by: pipeline
source_refs:
  - file_id: ab12
    quote: q
owner: someone-custom
---
body line 1

[link](/other.md)
"""


def test_parse_extracts_meta_and_body():
    meta, body = parse_note_text(SAMPLE)
    assert meta["type"] == "insight"          # lowercase-normalized
    assert meta["title"] == "t1"
    assert meta["id"] == "018f-xyz"
    assert meta["source_refs"][0]["file_id"] == "ab12"
    assert meta["owner"] == "someone-custom"  # unknown keys preserved (OKF is lenient)
    assert body.startswith("body line 1")


def test_parse_tolerates_missing_frontmatter():
    meta, body = parse_note_text("just a plain file\n")
    assert meta == {} and body == "just a plain file\n"


def test_parse_tolerates_broken_yaml():
    meta, body = parse_note_text("---\n: : :\n---\nbody\n")
    assert meta == {} and "body" in body


def test_serialize_roundtrip_and_key_order():
    meta, body = parse_note_text(SAMPLE)
    out = serialize_note_text(meta, body)
    lines = out.splitlines()
    assert lines[0] == "---"
    assert lines[1].startswith("type: Insight")      # capitalized first letter
    # known-key order: type comes first, custom owner comes after known keys
    assert out.index("type:") < out.index("id:") < out.index("owner:")
    meta2, body2 = parse_note_text(out)
    assert meta2 == meta and body2.strip() == body.strip()


def test_serialize_minimal_meta():
    out = serialize_note_text({"type": "note", "id": "x"}, "b\n")
    meta, body = parse_note_text(out)
    assert meta == {"type": "note", "id": "x"} and body == "b\n"


def test_timestamp_stays_string_and_roundtrips_literally():
    meta, _ = parse_note_text(SAMPLE)
    assert isinstance(meta["timestamp"], str)
    assert meta["timestamp"] == "2026-07-16T10:00:00+08:00"
    out = serialize_note_text(meta, "b\n")
    assert "2026-07-16T10:00:00+08:00" in out
