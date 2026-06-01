import json
from pathlib import Path

import pytest

from sandbox_run import build_sandbox_prompt


def _make_skill(root: Path, sid: str):
    d = root / sid
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "id": sid, "name": sid, "title": "T",
        "trigger": "auto", "color": "blue", "icon": "sparkle",
        "description": "desc", "version": "0.1.0", "author": "Nimo",
        "examples": [],
    }))
    (d / "SKILL.md").write_text(f"## {sid}\n\nrun stuff")


def test_build_sandbox_prompt_includes_md(tmp_path):
    builtin = tmp_path / "builtin"
    _make_skill(builtin, "photo-curator")
    prompt = build_sandbox_prompt(
        bundle_dir=str(builtin / "photo-curator"),
        user_prompt="Test it",
    )
    assert "photo-curator" in prompt
    assert "run stuff" in prompt
    assert "Test it" in prompt
