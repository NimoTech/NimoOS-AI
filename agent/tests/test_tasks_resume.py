"""compose_resume_message — the continuation instruction built at enqueue."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import json

from tasks import resume


def _run(**over):
    row = {"status": "failed", "error": "", "summary": "",
           "denied_actions": "[]"}
    row.update(over)
    return row


def test_supplement_rides_above_the_boilerplate():
    msg = resume.compose_resume_message(_run(error="disk full"),
                                        "clean /tmp first")
    assert msg.startswith("clean /tmp first")
    assert msg.index("clean /tmp first") < msg.index("disk full")
    assert "status: failed" in msg
    assert "update_task_prompt" in msg


def test_empty_fields_are_omitted():
    msg = resume.compose_resume_message(_run(status="timeout"))
    assert "status: timeout" in msg
    assert "Error:" not in msg and "final answer was" not in msg
    assert "denied" not in msg.lower()


def test_denied_actions_render_and_malformed_json_is_ignored():
    denied = json.dumps([{"kind": "shell", "detail": "rm -rf /x"},
                         "not-a-dict"])
    msg = resume.compose_resume_message(_run(denied_actions=denied))
    assert "shell rm -rf /x" in msg
    msg = resume.compose_resume_message(_run(denied_actions="{broken"))
    assert "denied" not in msg.lower()


def test_oversize_inputs_are_clipped():
    msg = resume.compose_resume_message(
        _run(error="e" * 9000), "s" * 9000)
    assert len(msg) < 8000
    assert "…" in msg
