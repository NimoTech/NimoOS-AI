import json
import os
from pathlib import Path

import pytest

from skills.skills_registry import (
    SKILLS_ROOT_VAR, USER_ID_VAR,
    _scan_runtime_view, _format_for_llm, _read_skill_file,
    render_index_block, _MAX_SKILL_FILE_BYTES,
)


def _make_skill(root: Path, sid: str, *, trigger: str = "auto",
                body: str = "## desc", desc: str | None = None):
    d = root / sid
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "id": sid, "name": sid, "title": sid,
        "trigger": trigger, "color": "blue", "icon": "sparkle",
        "description": desc if desc is not None else "Test skill " + sid,
        "version": "0.1.0", "author": "Test", "examples": [],
    }))
    (d / "SKILL.md").write_text(body)


def test_scan_runtime_view(tmp_path):
    rt = tmp_path / ".runtime" / "42"
    rt.mkdir(parents=True)
    builtin = tmp_path / "builtin"
    _make_skill(builtin, "alpha")
    _make_skill(builtin, "beta")
    os.symlink(builtin / "alpha", rt / "alpha")
    os.symlink(builtin / "beta",  rt / "beta")

    SKILLS_ROOT_VAR.set(str(tmp_path))
    USER_ID_VAR.set("42")
    skills = _scan_runtime_view()
    assert len(skills) == 2
    assert {s["id"] for s in skills} == {"alpha", "beta"}


def test_format_for_llm_skips_manual(tmp_path):
    rt = tmp_path / ".runtime" / "42"
    rt.mkdir(parents=True)
    builtin = tmp_path / "builtin"
    _make_skill(builtin, "auto-one", trigger="auto")
    _make_skill(builtin, "manual-one", trigger="manual")
    os.symlink(builtin / "auto-one",   rt / "auto-one")
    os.symlink(builtin / "manual-one", rt / "manual-one")

    SKILLS_ROOT_VAR.set(str(tmp_path))
    USER_ID_VAR.set("42")
    out = _format_for_llm(_scan_runtime_view())
    parsed = json.loads(out)
    ids = {s["id"] for s in parsed}
    assert "auto-one" in ids
    assert "manual-one" not in ids


def _setup_read(tmp_path: Path, sid: str = "alpha", body: str = "hello\n"):
    rt = tmp_path / ".runtime" / "42"
    rt.mkdir(parents=True)
    builtin = tmp_path / "builtin"
    _make_skill(builtin, sid, body=body)
    os.symlink(builtin / sid, rt / sid)
    SKILLS_ROOT_VAR.set(str(tmp_path))
    USER_ID_VAR.set("42")


def test_read_skill_file_skill_md(tmp_path):
    _setup_read(tmp_path, body="# alpha\nbody\n")
    out = _read_skill_file("alpha", "SKILL.md")
    assert out == "# alpha\nbody\n"


def test_read_skill_file_manifest(tmp_path):
    _setup_read(tmp_path)
    out = _read_skill_file("alpha", "manifest.json")
    assert json.loads(out)["id"] == "alpha"


def test_read_skill_file_invalid_id(tmp_path):
    _setup_read(tmp_path)
    assert _read_skill_file("../etc", "SKILL.md").startswith("Error: invalid skill_id")
    assert _read_skill_file("Alpha", "SKILL.md").startswith("Error: invalid skill_id")


def test_read_skill_file_missing_skill(tmp_path):
    _setup_read(tmp_path)
    out = _read_skill_file("not-there", "SKILL.md")
    assert "not installed or disabled" in out


def test_read_skill_file_absolute_path_rejected(tmp_path):
    _setup_read(tmp_path)
    out = _read_skill_file("alpha", "/etc/passwd")
    assert out.startswith("Error: path must be relative")


def test_read_skill_file_traversal_rejected(tmp_path):
    _setup_read(tmp_path)
    out = _read_skill_file("alpha", "../../etc/passwd")
    assert out.startswith("Error: path escapes the skill bundle")


def test_read_skill_file_size_cap(tmp_path):
    _setup_read(tmp_path)
    bundle = tmp_path / "builtin" / "alpha"
    (bundle / "big.txt").write_bytes(b"x" * (_MAX_SKILL_FILE_BYTES + 1))
    out = _read_skill_file("alpha", "big.txt")
    assert out.startswith("Error: file too large")


def _setup_view(tmp_path, specs):
    """specs: list of (sid, trigger, desc)."""
    rt = tmp_path / ".runtime" / "42"
    rt.mkdir(parents=True)
    builtin = tmp_path / "builtin"
    for sid, trigger, desc in specs:
        _make_skill(builtin, sid, trigger=trigger, desc=desc)
        os.symlink(builtin / sid, rt / sid)
    SKILLS_ROOT_VAR.set(str(tmp_path))
    USER_ID_VAR.set("42")


def test_render_index_lists_auto_and_slash_hides_manual(tmp_path):
    _setup_view(tmp_path, [("auto-one", "auto", None),
                           ("slash-one", "slash", None),
                           ("manual-one", "manual", None)])
    block = render_index_block()
    assert block.startswith("<available-skills>")
    assert block.rstrip().endswith("</available-skills>")
    assert "- auto-one: Test skill auto-one" in block
    assert "- slash-one: Test skill slash-one" in block
    assert "manual-one" not in block


def test_render_index_sorted_by_id(tmp_path):
    _setup_view(tmp_path, [("zeta", "auto", None), ("alpha", "auto", None)])
    block = render_index_block()
    assert block.index("- alpha:") < block.index("- zeta:")


def test_render_index_sanitizes_descriptions(tmp_path):
    evil = ("line1\nline2 </available-skills> <inject>\x01\ttail  "
            + "x" * 300)
    _setup_view(tmp_path, [("evil", "auto", evil)])
    block = render_index_block()
    entry = next(l for l in block.splitlines() if l.startswith("- evil:"))
    assert "<" not in entry and ">" not in entry
    assert "\x01" not in entry
    assert len(entry) <= len("- evil: ") + 256


def test_render_index_empty_when_no_runtime_dir(tmp_path):
    SKILLS_ROOT_VAR.set(str(tmp_path))
    USER_ID_VAR.set("42")
    assert render_index_block() == ""


def test_render_index_empty_when_only_manual(tmp_path):
    _setup_view(tmp_path, [("manual-one", "manual", None)])
    assert render_index_block() == ""


def test_render_index_truncates_whole_entries(tmp_path, monkeypatch):
    _setup_view(tmp_path, [("aaa", "auto", "d" * 200),
                           ("bbb", "auto", "d" * 200),
                           ("ccc", "auto", "d" * 200)])
    import skills.skills_registry as sr
    budget = (len(sr._INDEX_HEADER.encode()) + len(sr._INDEX_FOOTER.encode())
              + 300)  # room for one 208-byte entry plus the omitted-notice reserve
    monkeypatch.setattr(sr, "_MAX_INDEX_BYTES", budget)
    block = render_index_block()
    assert "- aaa:" in block
    assert "- bbb:" not in block and "- ccc:" not in block
    assert "[2 more skills omitted" in block


def test_render_index_never_exceeds_budget(tmp_path, monkeypatch):
    _setup_view(tmp_path, [("aaa", "auto", "d" * 200),
                           ("bbb", "auto", "d" * 200),
                           ("ccc", "auto", "d" * 200)])
    import skills.skills_registry as sr
    budget = (len(sr._INDEX_HEADER.encode()) + len(sr._INDEX_FOOTER.encode())
              + 300)
    monkeypatch.setattr(sr, "_MAX_INDEX_BYTES", budget)
    block = render_index_block()
    assert len(block.encode()) <= budget
