import json
import audit as A


def test_writes_one_json_line(tmp_path):
    p = tmp_path / "audit.log"
    A.set_audit_path_for_test(str(p))
    A.audit("shell_command", user_id="u1", session_id="s1",
            command="rm -rf /x", level="dangerous", outcome="refused")
    lines = p.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event"] == "shell_command"
    assert rec["user_id"] == "u1" and rec["session_id"] == "s1"
    assert rec["command"] == "rm -rf /x"
    assert rec["outcome"] == "refused"
    assert isinstance(rec["ts"], int)


def test_appends_never_truncates(tmp_path):
    p = tmp_path / "audit.log"
    A.set_audit_path_for_test(str(p))
    A.audit("a", user_id="u1")
    A.audit("b", user_id="u1")
    lines = p.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "a"
    assert json.loads(lines[1])["event"] == "b"


def test_never_raises_on_bad_path(monkeypatch):
    # unwritable path must not raise out of audit()
    A.set_audit_path_for_test("/nonexistent-dir-xyz/audit.log")
    A.audit("x", user_id="u1")  # must not raise


def test_non_serializable_field_does_not_raise(tmp_path):
    p = tmp_path / "audit.log"
    A.set_audit_path_for_test(str(p))
    A.audit("x", user_id="u1", weird=object())  # must not raise; line still written
    assert p.exists()
