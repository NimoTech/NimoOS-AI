from __future__ import annotations
import json
from wiki_summary_worker import prompt, sampler


def test_system_prompt_has_required_directives():
    s = prompt.SYSTEM
    assert "JSON" in s
    assert "ai_label" in s
    assert "summary" in s
    assert "40" in s, "ai_label length cap should be stated"
    assert "400" in s, "summary length cap should be stated"
    assert "markdown" in s.lower()


def test_system_prompt_size_under_2kb():
    """System prompt is sent on every LLM call; keep it tight."""
    assert len(prompt.SYSTEM.encode("utf-8")) < 2000


def test_serialize_evidence_round_trip():
    ev = sampler.Evidence(
        node_path="/x",
        child_map=[{"name": "a.md", "size": 12, "is_dir": False, "ext": "md"}],
        text_files=[sampler.FileExcerpt(relpath="a.md", bytes=12, content="hi")],
    )
    payload = prompt.serialize_user_message(ev)
    parsed = json.loads(payload)
    assert parsed["node_path"] == "/x"
    assert parsed["text_files"][0]["content"] == "hi"
