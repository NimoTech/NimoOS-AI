"""fold_denied — the pure fold shared by the from-denied endpoint and the
channel escalation's persist button. One source of truth for how a denied
action becomes a preauth rule."""
import pytest

from tasks import preauth


def _doc():
    return preauth.parse({})


def test_egress_strips_port():
    doc, bucket, entry = preauth.fold_denied(
        _doc(), {"kind": "egress", "detail": "open.feishu.cn:443"})
    assert bucket == "egress_domains" and entry == "open.feishu.cn"
    assert "open.feishu.cn" in doc["egress_domains"]


def test_fs_file_becomes_parent_dir(tmp_path):
    f = tmp_path / "report.txt"
    f.write_text("x")
    doc, bucket, entry = preauth.fold_denied(
        _doc(), {"kind": "fs", "detail": str(f)})
    assert bucket == "fs_write" and entry == str(tmp_path)


def test_fs_system_root_rejected():
    with pytest.raises(preauth.FoldError) as ei:
        preauth.fold_denied(_doc(), {"kind": "fs", "detail": "/etc/passwd"})
    assert ei.value.reason == "bad_fs_write"


def test_fs_relative_rejected():
    with pytest.raises(preauth.FoldError) as ei:
        preauth.fold_denied(_doc(), {"kind": "fs", "detail": "reports"})
    assert ei.value.reason == "bad_fs_write"


def test_mcp_tool_passthrough():
    doc, bucket, entry = preauth.fold_denied(
        _doc(), {"kind": "mcp_tool", "detail": "srv1::search"})
    assert bucket == "mcp_tools" and entry == "srv1::search"


def test_shell_simple_command_becomes_prefix():
    doc, bucket, entry = preauth.fold_denied(
        _doc(), {"kind": "shell", "detail": "gh pr list --limit 5"})
    assert bucket == "shell"
    assert entry == {"kind": "prefix", "value": "gh "}


def test_shell_script_run_becomes_scripts_entry(tmp_path):
    script = tmp_path / "radar.py"
    script.write_text("print('x')")
    doc, bucket, entry = preauth.fold_denied(
        _doc(), {"kind": "shell", "detail": f"python3 {script}"})
    assert bucket == "scripts" and entry == str(script)


def test_shell_chained_command_rejected():
    with pytest.raises(preauth.FoldError) as ei:
        preauth.fold_denied(_doc(), {"kind": "shell", "detail": "a && b"})
    assert ei.value.reason == "shell_rule_would_not_apply"


def test_unsupported_kind_and_empty_detail():
    with pytest.raises(preauth.FoldError) as ei:
        preauth.fold_denied(_doc(), {"kind": "elicitation", "detail": "x"})
    assert ei.value.reason == "unsupported_kind"
    with pytest.raises(preauth.FoldError) as ei:
        preauth.fold_denied(_doc(), {"kind": "egress", "detail": "  "})
    assert ei.value.reason == "empty_detail"


def test_input_doc_not_mutated():
    doc = _doc()
    preauth.fold_denied(doc, {"kind": "egress", "detail": "a.com"})
    assert doc["egress_domains"] == []
