import json
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import notes_distill


def test_chunk_text_splits_and_caps():
    text = "x" * (notes_distill.CHUNK_CHARS * 12)
    chunks = notes_distill.chunk_text(text)
    assert len(chunks) == notes_distill.MAX_CHUNKS
    assert all(len(c) == notes_distill.CHUNK_CHARS for c in chunks)


def test_chunk_text_short_input_is_single_chunk():
    assert notes_distill.chunk_text("hello") == ["hello"]


def test_chunk_text_empty_input_is_empty_list():
    assert notes_distill.chunk_text("") == []


def test_extract_budget_matches_chunk_cap():
    assert notes_distill.EXTRACT_MAX_CHARS == \
        notes_distill.CHUNK_CHARS * notes_distill.MAX_CHUNKS


def test_summary_prompt_carries_filename_and_json_contract():
    p = notes_distill.build_summary_prompt("body text", filename="contract.pdf")
    assert "contract.pdf" in p
    assert '"title"' in p and '"body"' in p
    assert "body text" in p


def test_map_prompt_states_part_position():
    p = notes_distill.build_map_prompt("chunk", 2, 5, filename="a.pdf")
    assert "part 3 of 5" in p


def test_reduce_prompt_joins_partials():
    p = notes_distill.build_reduce_prompt(["one", "two"], filename="a.pdf")
    assert "one" in p and "two" in p


def test_parse_summary_accepts_fenced_json():
    raw = "```json\n" + json.dumps({
        "title": "T", "description": "D", "body": "B", "tags": ["a", 1, " "]
    }) + "\n```"
    out = notes_distill.parse_summary(raw)
    assert out == {"title": "T", "description": "D", "body": "B",
                   "tags": ["a"]}


def test_parse_summary_rejects_missing_body():
    assert notes_distill.parse_summary(json.dumps({"title": "T"})) is None


def test_parse_summary_rejects_non_json():
    assert notes_distill.parse_summary("sorry, I cannot") is None


def test_parse_summary_accepts_prose_wrapped_json():
    raw = "Here is the summary in JSON format:\n\n" + json.dumps({
        "title": "T", "description": "D", "body": "B", "tags": ["a"]
    })
    out = notes_distill.parse_summary(raw)
    assert out == {"title": "T", "description": "D", "body": "B",
                   "tags": ["a"]}


def test_parse_summary_accepts_json_with_trailing_prose():
    raw = json.dumps({
        "title": "T", "description": "D", "body": "B", "tags": ["a"]
    }) + "\n\nLet me know if you need anything else!"
    out = notes_distill.parse_summary(raw)
    assert out == {"title": "T", "description": "D", "body": "B",
                   "tags": ["a"]}


def test_parse_summary_still_rejects_pure_prose():
    assert notes_distill.parse_summary(
        "I cannot summarize this document.") is None
