import json
import os
from pathlib import Path

import pytest

from skills.skills_registry import (
    SKILLS_ROOT_VAR, USER_ID_VAR,
    _scan_runtime_view, _format_for_llm,
)


def _make_skill(root: Path, sid: str, *, trigger: str = "auto", body: str = "## desc"):
    d = root / sid
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "id": sid, "name": sid, "title": sid,
        "trigger": trigger, "color": "blue", "icon": "sparkle",
        "description": "Test skill " + sid, "version": "0.1.0",
        "author": "Test", "examples": [],
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
