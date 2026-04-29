# Markdown Skills — 后端核心实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `NimoOS-AI/agent` 上落地用户级 markdown skill 的存储层 + 工具 + 系统提示词注入，使得用户在文件系统里有 skill 时，下次发消息就能在系统提示词索引里看到，并通过 `use_skill` / `read_skill_resource` 渐进式加载。

**Architecture:** 用户 skill 落盘到 `~/.nimoos/agent/user_skills/<user_id>/<skill_name>/`；不入 DB。每次 `AgentRunner.run()` 扫描该用户目录，把 frontmatter 的 `(name, description)` 拼入 system prompt；模型按需调 `use_skill`/`read_skill_resource` 工具加载正文与资源。写入采用 `.tmp-<uuid>` → `rename` 原子化，启动时 sweep 残留。

**Tech Stack:** Python 3.13、`PyYAML`（新增依赖）、`openai-agents` SDK（已有）、`pytest` + `pytest-asyncio`（已有）。

**关联文档：**
- 设计：`docs/superpowers/specs/2026-04-29-markdown-skills-design.md`
- 现状：`docs/superpowers/specs/2026-04-29-skills-architecture-current.md`

**本计划范围：** 设计文档第 11 节中的步骤 1（存储层）+ 步骤 2（工具与提示词注入）。
**不在本计划范围：** HTTP API（步骤 3）、前端 UI（步骤 4）。

**测试运行约定：** 进入 `NimoOS-AI/agent/` 目录，激活 venv：
```bash
cd NimoOS-AI/agent && source venv/bin/activate
pytest tests/ -v
```

**目录结构（本计划新增）：**

```
agent/
  user_skills/
    __init__.py
    paths.py        # 名称/路径/描述校验、配额常量
    loader.py       # frontmatter 解析、索引扫描、正文读取
    storage.py      # 创建/更新/删除（原子 rename + 配额）
    sweeper.py      # 启动期清理 .tmp-* / .old-*
    context.py      # USER_ID_VAR + SKILLS_ROOT_VAR
    tools.py        # use_skill / read_skill_resource FunctionTool
  tests/
    test_user_skills_paths.py
    test_user_skills_loader.py
    test_user_skills_storage.py
    test_user_skills_sweeper.py
    test_user_skills_tools.py
    test_user_skills_integration.py
```

---

## Task 1: 依赖 + paths.py 校验工具

**Files:**
- Modify: `NimoOS-AI/agent/requirements.txt`
- Create: `NimoOS-AI/agent/user_skills/__init__.py`
- Create: `NimoOS-AI/agent/user_skills/paths.py`
- Test: `NimoOS-AI/agent/tests/test_user_skills_paths.py`

- [ ] **Step 1: 添加 PyYAML 到 requirements.txt**

在文件末尾追加一行：

```
PyYAML>=6.0
```

随后 `pip install -r requirements.txt` 安装。

- [ ] **Step 2: 创建空的包入口**

`agent/user_skills/__init__.py`：

```python
"""User-defined markdown skills (per-user, on-disk, hot-reloaded)."""
```

- [ ] **Step 3: 写失败测试**

`agent/tests/test_user_skills_paths.py`：

```python
import pytest

from user_skills.paths import (
    DESCRIPTION_MAX, RESOURCE_FILE_MAX, SKILL_BODY_MAX,
    SKILL_FILE_COUNT_MAX, SKILL_TOTAL_MAX, USER_SKILL_COUNT_MAX,
    validate_skill_name, validate_user_id, validate_description,
    validate_resource_path,
)


@pytest.mark.parametrize("name", [
    "ab", "skill-name", "weekly-report", "x9", "a0b1c2",
])
def test_skill_name_accepts_valid(name):
    assert validate_skill_name(name) == name


@pytest.mark.parametrize("name", [
    "", "a", "A", "Skill", "-name", "name-", "skill_name",
    "../etc", "/abs", ".hidden", "a" * 33, "skill name",
])
def test_skill_name_rejects_invalid(name):
    with pytest.raises(ValueError):
        validate_skill_name(name)


def test_user_id_basic():
    assert validate_user_id("1") == "1"
    assert validate_user_id("user_42") == "user_42"


@pytest.mark.parametrize("uid", ["", "u/1", "u 1", "u\n1", "a" * 65])
def test_user_id_rejects_invalid(uid):
    with pytest.raises(ValueError):
        validate_user_id(uid)


def test_description_basic():
    assert validate_description("Generate a weekly report") == "Generate a weekly report"


@pytest.mark.parametrize("desc,reason", [
    ("a" * (DESCRIPTION_MAX + 1), "too long"),
    ("line1\nline2", "newline"),
    ("has <tag>", "angle bracket"),
    ("ends-with-cr\r", "carriage return"),
    ("ctrl\x07char", "control"),
])
def test_description_rejects_unsafe(desc, reason):
    with pytest.raises(ValueError):
        validate_description(desc)


@pytest.mark.parametrize("rel", [
    "SKILL.md", "scripts/foo.sh", "references/api.md",
    "deeply/nested/dir/note.txt",
])
def test_resource_path_accepts_valid(rel):
    assert validate_resource_path(rel) == rel


@pytest.mark.parametrize("rel", [
    "", "/abs", "../escape", "scripts/../etc/passwd",
    "a/./b", ".hidden/x.md", "scripts/.env",
    "back\\slash.md", "noext", "binary.png", "weird.exe",
])
def test_resource_path_rejects_invalid(rel):
    with pytest.raises(ValueError):
        validate_resource_path(rel)


def test_constants_present():
    assert DESCRIPTION_MAX == 256
    assert SKILL_BODY_MAX == 32 * 1024
    assert RESOURCE_FILE_MAX == 256 * 1024
    assert SKILL_TOTAL_MAX == 1024 * 1024
    assert SKILL_FILE_COUNT_MAX == 100
    assert USER_SKILL_COUNT_MAX == 50
```

- [ ] **Step 4: 运行测试确认全部失败**

```bash
cd NimoOS-AI/agent && pytest tests/test_user_skills_paths.py -v
```

Expected: ImportError / ModuleNotFoundError on `user_skills.paths`.

- [ ] **Step 5: 实现 paths.py**

`agent/user_skills/paths.py`：

```python
"""Path / name / description validation for user-uploaded skills.

All public helpers raise ValueError with a user-facing message when the
input is invalid; HTTP handlers can surface the message in 4xx bodies.
"""
from __future__ import annotations

import re
import unicodedata

# Skill name: lowercase, kebab-case, 2-32 chars, must start and end alnum.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$")

# user_id alphabet: alphanumeric + _ -, length 1-64. Matches what main.py's
# X-User-Id values look like in this codebase (typically a numeric id).
_USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

ALLOWED_EXTS = frozenset({
    ".md", ".txt", ".sh", ".py", ".js", ".ts", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".conf", ".sql",
    ".html", ".css",
})

DESCRIPTION_MAX = 256
SKILL_BODY_MAX = 32 * 1024
RESOURCE_FILE_MAX = 256 * 1024
SKILL_TOTAL_MAX = 1024 * 1024
SKILL_FILE_COUNT_MAX = 100
USER_SKILL_COUNT_MAX = 50


def validate_skill_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(
            "skill name must match ^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$"
        )
    return name


def validate_user_id(user_id: str) -> str:
    if not isinstance(user_id, str) or not _USER_ID_RE.match(user_id):
        raise ValueError("user_id contains invalid characters")
    return user_id


def validate_description(description: str) -> str:
    if not isinstance(description, str):
        raise ValueError("description must be a string")
    if len(description) > DESCRIPTION_MAX:
        raise ValueError(f"description exceeds {DESCRIPTION_MAX} chars")
    if "\n" in description or "\r" in description:
        raise ValueError("description must be a single line")
    if "<" in description or ">" in description:
        raise ValueError("description must not contain '<' or '>'")
    for ch in description:
        # Reject all C0/C1 control chars except space; space is " " (not Cc).
        if unicodedata.category(ch) in ("Cc", "Cf"):
            raise ValueError("description contains control characters")
    return description


def validate_resource_path(rel: str) -> str:
    """Validate a relative path inside a skill bundle. Returns the
    POSIX-normalized form. Rejects: absolute, backslash, '..', '.',
    empty segments, dot-prefixed segments, disallowed extensions."""
    if not isinstance(rel, str) or not rel:
        raise ValueError("resource path is required")
    if rel.startswith("/") or "\\" in rel:
        raise ValueError("resource path must be relative POSIX form")
    parts = rel.split("/")
    for p in parts:
        if not p or p in (".", ".."):
            raise ValueError("resource path contains invalid segment")
        if p.startswith("."):
            raise ValueError("resource path segments must not start with '.'")
    lower = rel.lower()
    if not any(lower.endswith(ext) for ext in ALLOWED_EXTS):
        raise ValueError("resource extension not allowed")
    return "/".join(parts)
```

- [ ] **Step 6: 运行测试确认通过**

```bash
pytest tests/test_user_skills_paths.py -v
```

Expected: 全部 PASS。

- [ ] **Step 7: Commit**

```bash
git add NimoOS-AI/agent/requirements.txt \
        NimoOS-AI/agent/user_skills/__init__.py \
        NimoOS-AI/agent/user_skills/paths.py \
        NimoOS-AI/agent/tests/test_user_skills_paths.py
git commit -m "feat(user-skills): add path/name/description validators"
```

---

## Task 2: loader.py — frontmatter 解析与索引扫描

**Files:**
- Create: `NimoOS-AI/agent/user_skills/loader.py`
- Test: `NimoOS-AI/agent/tests/test_user_skills_loader.py`

- [ ] **Step 1: 写失败测试**

`agent/tests/test_user_skills_loader.py`：

```python
from pathlib import Path
import pytest

from user_skills.loader import (
    SkillEntry, parse_frontmatter, list_skill_index, read_skill_body,
)


def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


GOOD_MD = (
    "---\n"
    "name: weekly-report\n"
    "description: Generate a Markdown weekly report.\n"
    "---\n"
    "Body line 1\nBody line 2\n"
)


def test_parse_frontmatter_basic():
    fm, body = parse_frontmatter(GOOD_MD)
    assert fm == {"name": "weekly-report",
                  "description": "Generate a Markdown weekly report."}
    assert body == "Body line 1\nBody line 2\n"


def test_parse_frontmatter_missing_open():
    with pytest.raises(ValueError):
        parse_frontmatter("no frontmatter here")


def test_parse_frontmatter_unclosed():
    with pytest.raises(ValueError):
        parse_frontmatter("---\nname: x\nno-close\n")


def test_parse_frontmatter_invalid_yaml():
    bad = "---\nname: [unbalanced\n---\nbody\n"
    with pytest.raises(ValueError):
        parse_frontmatter(bad)


def test_parse_frontmatter_yaml_must_be_mapping():
    bad = "---\n- list item\n---\nbody\n"
    with pytest.raises(ValueError):
        parse_frontmatter(bad)


def test_list_skill_index_empty(tmp_path):
    assert list_skill_index(tmp_path, "1") == []


def test_list_skill_index_finds_visible_only(tmp_path):
    base = tmp_path / "1"
    _write(base / "weekly-report" / "SKILL.md", GOOD_MD)
    _write(base / ".tmp-half" / "SKILL.md", GOOD_MD)
    _write(base / ".old-x" / "SKILL.md", GOOD_MD)
    _write(base / ".hidden" / "SKILL.md", GOOD_MD)
    (base / "no-skill-md").mkdir()
    out = list_skill_index(tmp_path, "1")
    assert out == [SkillEntry(name="weekly-report",
                              description="Generate a Markdown weekly report.")]


def test_list_skill_index_skips_bad_yaml(tmp_path, caplog):
    base = tmp_path / "1"
    _write(base / "good" / "SKILL.md",
           GOOD_MD.replace("weekly-report", "good"))
    _write(base / "bad" / "SKILL.md",
           "---\nname: [unbalanced\n---\nbody\n")
    out = list_skill_index(tmp_path, "1")
    names = [e.name for e in out]
    assert names == ["good"]


def test_list_skill_index_skips_name_mismatch(tmp_path):
    base = tmp_path / "1"
    # frontmatter says 'a' but dir is 'b' — skip.
    _write(base / "b" / "SKILL.md",
           GOOD_MD.replace("weekly-report", "a"))
    assert list_skill_index(tmp_path, "1") == []


def test_read_skill_body_returns_body(tmp_path):
    base = tmp_path / "1" / "weekly-report"
    _write(base / "SKILL.md", GOOD_MD)
    assert read_skill_body(tmp_path, "1", "weekly-report") == \
        "Body line 1\nBody line 2\n"


def test_read_skill_body_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_skill_body(tmp_path, "1", "weekly-report")
```

- [ ] **Step 2: 运行确认失败**

```bash
pytest tests/test_user_skills_loader.py -v
```

Expected: ImportError on `user_skills.loader`.

- [ ] **Step 3: 实现 loader.py**

`agent/user_skills/loader.py`：

```python
"""Read user skills from disk: frontmatter parsing + index scan + body."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import NamedTuple

import yaml

from .paths import validate_skill_name, validate_user_id

log = logging.getLogger(__name__)


class SkillEntry(NamedTuple):
    name: str
    description: str


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a SKILL.md string into (frontmatter_dict, body).

    Frontmatter must start at byte 0 with '---\\n' and close with a line
    containing only '---'. Anything else raises ValueError.
    """
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md missing leading frontmatter '---'")
    rest = text[4:]
    end = rest.find("\n---\n")
    if end >= 0:
        fm_text = rest[:end]
        body = rest[end + 5:]
    elif rest.endswith("\n---"):
        fm_text = rest[:-4]
        body = ""
    else:
        raise ValueError("SKILL.md frontmatter not closed by '---'")
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"SKILL.md frontmatter not valid YAML: {e}") from e
    if not isinstance(fm, dict):
        raise ValueError("SKILL.md frontmatter must be a YAML mapping")
    return fm, body


def _user_dir(root: Path, user_id: str) -> Path:
    return Path(root) / validate_user_id(user_id)


def _readable_skill_dirs(root: Path, user_id: str) -> list[Path]:
    base = _user_dir(root, user_id)
    if not base.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(base.iterdir()):
        if not p.is_dir():
            continue
        n = p.name
        if n.startswith("."):
            continue
        if not (p / "SKILL.md").is_file():
            continue
        out.append(p)
    return out


def list_skill_index(root: Path, user_id: str) -> list[SkillEntry]:
    """Return (name, description) for each visible, parseable skill.
    Bad skills are silently skipped (logged at WARNING)."""
    out: list[SkillEntry] = []
    for d in _readable_skill_dirs(root, user_id):
        try:
            text = (d / "SKILL.md").read_text(encoding="utf-8")
            fm, _ = parse_frontmatter(text)
        except (UnicodeDecodeError, OSError, ValueError) as e:
            log.warning("skipping skill %s/%s: %s", user_id, d.name, e)
            continue
        name = fm.get("name")
        desc = fm.get("description")
        if not isinstance(name, str) or not isinstance(desc, str):
            log.warning("skill %s/%s missing name/description", user_id, d.name)
            continue
        if name != d.name:
            log.warning("skill %s/%s frontmatter name mismatch", user_id, d.name)
            continue
        out.append(SkillEntry(name=name, description=desc))
    return out


def read_skill_body(root: Path, user_id: str, name: str) -> str:
    """Return the SKILL.md body (without frontmatter) for a single skill.
    Caller is responsible for catching FileNotFoundError / ValueError /
    UnicodeDecodeError."""
    target = _user_dir(root, user_id) / validate_skill_name(name) / "SKILL.md"
    text = target.read_text(encoding="utf-8")
    _, body = parse_frontmatter(text)
    return body
```

- [ ] **Step 4: 运行确认通过**

```bash
pytest tests/test_user_skills_loader.py -v
```

Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add NimoOS-AI/agent/user_skills/loader.py \
        NimoOS-AI/agent/tests/test_user_skills_loader.py
git commit -m "feat(user-skills): frontmatter parser + index scanner"
```

---

## Task 3: storage.py — 原子 CRUD + 配额校验

**Files:**
- Create: `NimoOS-AI/agent/user_skills/storage.py`
- Test: `NimoOS-AI/agent/tests/test_user_skills_storage.py`

- [ ] **Step 1: 写失败测试**

`agent/tests/test_user_skills_storage.py`：

```python
from pathlib import Path
import pytest

from user_skills import storage
from user_skills.paths import (
    RESOURCE_FILE_MAX, SKILL_BODY_MAX, SKILL_FILE_COUNT_MAX,
    SKILL_TOTAL_MAX, USER_SKILL_COUNT_MAX,
)


def _md(name: str, desc: str = "Do a thing.", body: str = "Body.") -> str:
    return f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n"


def test_create_writes_files(tmp_path):
    files = {"SKILL.md": _md("skill-a"),
             "scripts/foo.sh": "#!/bin/bash\necho hi\n"}
    storage.create_skill(tmp_path, "1", "skill-a", files)
    sd = tmp_path / "1" / "skill-a"
    assert sd.is_dir()
    assert (sd / "SKILL.md").read_text() == files["SKILL.md"]
    assert (sd / "scripts" / "foo.sh").read_text() == files["scripts/foo.sh"]


def test_create_rejects_duplicate(tmp_path):
    storage.create_skill(tmp_path, "1", "a", {"SKILL.md": _md("a")})
    with pytest.raises(FileExistsError):
        storage.create_skill(tmp_path, "1", "a", {"SKILL.md": _md("a")})


def test_create_requires_skill_md(tmp_path):
    with pytest.raises(ValueError, match="SKILL.md"):
        storage.create_skill(tmp_path, "1", "a",
                             {"scripts/foo.sh": "echo hi\n"})


def test_create_rejects_name_mismatch(tmp_path):
    with pytest.raises(ValueError):
        storage.create_skill(tmp_path, "1", "a", {"SKILL.md": _md("b")})


def test_create_rejects_bad_description(tmp_path):
    bad = ("---\nname: a\ndescription: contains </closing>\n---\nBody\n")
    with pytest.raises(ValueError):
        storage.create_skill(tmp_path, "1", "a", {"SKILL.md": bad})


def test_create_rejects_oversized_body(tmp_path):
    body = "x" * (SKILL_BODY_MAX + 1)
    with pytest.raises(ValueError, match="body"):
        storage.create_skill(tmp_path, "1", "a",
                             {"SKILL.md": _md("a", body=body)})


def test_create_rejects_oversized_file(tmp_path):
    big = "y" * (RESOURCE_FILE_MAX + 1)
    with pytest.raises(ValueError):
        storage.create_skill(tmp_path, "1", "a",
                             {"SKILL.md": _md("a"),
                              "scripts/big.sh": big})


def test_create_rejects_too_many_files(tmp_path):
    files = {"SKILL.md": _md("a")}
    for i in range(SKILL_FILE_COUNT_MAX):  # +1 with SKILL.md = over
        files[f"references/r{i}.md"] = "x"
    with pytest.raises(ValueError):
        storage.create_skill(tmp_path, "1", "a", files)


def test_create_rejects_total_size(tmp_path):
    # 5 files each ~200 KiB + SKILL.md → > 1 MiB
    files = {"SKILL.md": _md("a")}
    chunk = "z" * (200 * 1024)
    for i in range(6):
        files[f"references/r{i}.md"] = chunk
    with pytest.raises(ValueError, match="total"):
        storage.create_skill(tmp_path, "1", "a", files)


def test_create_rejects_user_quota(tmp_path):
    for i in range(USER_SKILL_COUNT_MAX):
        n = f"s{i:02d}"
        storage.create_skill(tmp_path, "1", n, {"SKILL.md": _md(n)})
    with pytest.raises(ValueError, match="exceed"):
        storage.create_skill(tmp_path, "1", "s99", {"SKILL.md": _md("s99")})


def test_create_isolated_per_user(tmp_path):
    storage.create_skill(tmp_path, "1", "a", {"SKILL.md": _md("a")})
    storage.create_skill(tmp_path, "2", "a", {"SKILL.md": _md("a")})
    assert (tmp_path / "1" / "a" / "SKILL.md").is_file()
    assert (tmp_path / "2" / "a" / "SKILL.md").is_file()


def test_update_replaces_atomically(tmp_path):
    storage.create_skill(tmp_path, "1", "a",
                         {"SKILL.md": _md("a", body="v1"),
                          "scripts/old.sh": "old\n"})
    storage.update_skill(tmp_path, "1", "a",
                         {"SKILL.md": _md("a", body="v2")})
    sd = tmp_path / "1" / "a"
    assert "v2" in (sd / "SKILL.md").read_text()
    # old resource gone after replacement
    assert not (sd / "scripts" / "old.sh").exists()


def test_update_keeps_count_at_quota(tmp_path):
    """At quota limit, PUT must not be rejected."""
    for i in range(USER_SKILL_COUNT_MAX):
        n = f"s{i:02d}"
        storage.create_skill(tmp_path, "1", n, {"SKILL.md": _md(n)})
    # Updating an existing one should succeed.
    storage.update_skill(tmp_path, "1", "s00",
                         {"SKILL.md": _md("s00", body="updated")})
    body = (tmp_path / "1" / "s00" / "SKILL.md").read_text()
    assert "updated" in body


def test_update_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        storage.update_skill(tmp_path, "1", "ghost",
                             {"SKILL.md": _md("ghost")})


def test_delete_removes(tmp_path):
    storage.create_skill(tmp_path, "1", "a", {"SKILL.md": _md("a")})
    assert storage.delete_skill(tmp_path, "1", "a") is True
    assert not (tmp_path / "1" / "a").exists()


def test_delete_missing_returns_false(tmp_path):
    assert storage.delete_skill(tmp_path, "1", "ghost") is False


def test_create_rejects_non_utf8(tmp_path):
    """The contract is files map[str]→str so non-UTF-8 should already be a
    string-encoding error at the JSON layer; we still defend by ensuring
    contents round-trip cleanly."""
    files = {"SKILL.md": _md("a"),
             "scripts/foo.sh": "\udce4\udcb8\udcad"}  # surrogate (invalid UTF-8)
    with pytest.raises(ValueError, match="UTF-8"):
        storage.create_skill(tmp_path, "1", "a", files)


def test_create_rolls_back_tmp_on_failure(tmp_path, monkeypatch):
    """If a mid-write OSError happens, the .tmp-* dir must be cleaned up."""
    def fail_open(*a, **kw):
        raise OSError("disk full")
    real_open = open
    calls = {"n": 0}

    def maybe_fail(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated")
        return real_open(*a, **kw)

    monkeypatch.setattr("builtins.open", maybe_fail)
    with pytest.raises(OSError):
        storage.create_skill(tmp_path, "1", "a",
                             {"SKILL.md": _md("a"),
                              "scripts/foo.sh": "echo hi\n"})
    # Target not created; no .tmp-* leftover
    user_dir = tmp_path / "1"
    if user_dir.exists():
        leftovers = [p.name for p in user_dir.iterdir()
                     if p.name.startswith(".tmp-")]
        assert leftovers == []
    assert not (tmp_path / "1" / "a").exists()
```

- [ ] **Step 2: 运行确认失败**

```bash
pytest tests/test_user_skills_storage.py -v
```

Expected: ImportError on `user_skills.storage`.

- [ ] **Step 3: 实现 storage.py**

`agent/user_skills/storage.py`：

```python
"""User skill on-disk CRUD with atomic rename + quota enforcement."""
from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path

from .loader import parse_frontmatter
from .paths import (
    DESCRIPTION_MAX, RESOURCE_FILE_MAX, SKILL_BODY_MAX,
    SKILL_FILE_COUNT_MAX, SKILL_TOTAL_MAX, USER_SKILL_COUNT_MAX,
    validate_description, validate_resource_path,
    validate_skill_name, validate_user_id,
)

log = logging.getLogger(__name__)

TMP_PREFIX = ".tmp-"
OLD_PREFIX = ".old-"


def _user_dir(root: Path, user_id: str) -> Path:
    return Path(root) / validate_user_id(user_id)


def _skill_dir(root: Path, user_id: str, name: str) -> Path:
    return _user_dir(root, user_id) / validate_skill_name(name)


def _is_visible_skill(p: Path) -> bool:
    n = p.name
    if n.startswith("."):
        return False
    return (p / "SKILL.md").is_file()


def list_skill_dirs(root: Path, user_id: str) -> list[Path]:
    base = _user_dir(root, user_id)
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir()
                  if p.is_dir() and _is_visible_skill(p))


def _validate_files_map(files: dict, name: str) -> None:
    if not isinstance(files, dict):
        raise ValueError("files must be a mapping")
    if "SKILL.md" not in files:
        raise ValueError("files must contain SKILL.md")
    if len(files) > SKILL_FILE_COUNT_MAX:
        raise ValueError(f"file count exceeds {SKILL_FILE_COUNT_MAX}")
    total = 0
    for rel, content in files.items():
        validate_resource_path(rel)
        if not isinstance(content, str):
            raise ValueError(f"{rel}: content must be a string")
        try:
            blob = content.encode("utf-8", errors="strict")
        except UnicodeEncodeError as e:
            raise ValueError(f"{rel}: not valid UTF-8: {e}")
        if len(blob) > RESOURCE_FILE_MAX:
            raise ValueError(
                f"{rel}: file exceeds {RESOURCE_FILE_MAX} bytes"
            )
        total += len(blob)
    if total > SKILL_TOTAL_MAX:
        raise ValueError(f"skill total size exceeds {SKILL_TOTAL_MAX} bytes")
    fm, body = parse_frontmatter(files["SKILL.md"])
    fm_name = fm.get("name")
    if not isinstance(fm_name, str) or fm_name != name:
        raise ValueError(
            "SKILL.md frontmatter 'name' must equal URL name"
        )
    desc = fm.get("description")
    if not isinstance(desc, str):
        raise ValueError("SKILL.md frontmatter 'description' is required")
    validate_description(desc)
    if len(body.encode("utf-8")) > SKILL_BODY_MAX:
        raise ValueError(f"SKILL.md body exceeds {SKILL_BODY_MAX} bytes")


def _atomic_write_skill(target: Path, files: dict) -> None:
    """Write `files` into `target`, replacing any prior contents atomically.
    On any error, removes the staging dir and (best-effort) restores the
    pre-existing target."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f"{TMP_PREFIX}{target.name}-{uuid.uuid4().hex}"
    parked: Path | None = None
    try:
        tmp.mkdir()
        for rel, content in files.items():
            dst = tmp / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            with open(dst, "w", encoding="utf-8", newline="") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
        if target.exists():
            parked = target.parent / f"{OLD_PREFIX}{target.name}-{uuid.uuid4().hex}"
            target.rename(parked)
        tmp.rename(target)
        if parked is not None:
            shutil.rmtree(parked, ignore_errors=True)
            parked = None
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        if parked is not None and not target.exists():
            try:
                parked.rename(target)
            except OSError:
                pass
        raise


def create_skill(root: Path, user_id: str, name: str, files: dict) -> None:
    target = _skill_dir(root, user_id, name)
    if target.exists():
        raise FileExistsError(f"skill {name!r} already exists")
    existing = list_skill_dirs(root, user_id)
    if len(existing) + 1 > USER_SKILL_COUNT_MAX:
        raise ValueError(
            f"user skill count would exceed {USER_SKILL_COUNT_MAX}"
        )
    _validate_files_map(files, name)
    _atomic_write_skill(target, files)


def update_skill(root: Path, user_id: str, name: str, files: dict) -> None:
    target = _skill_dir(root, user_id, name)
    if not target.exists():
        raise FileNotFoundError(f"skill {name!r} not found")
    _validate_files_map(files, name)
    _atomic_write_skill(target, files)


def delete_skill(root: Path, user_id: str, name: str) -> bool:
    target = _skill_dir(root, user_id, name)
    if not target.exists():
        return False
    parking = target.parent / f"{OLD_PREFIX}{target.name}-{uuid.uuid4().hex}"
    target.rename(parking)
    shutil.rmtree(parking, ignore_errors=True)
    return True
```

- [ ] **Step 4: 运行确认通过**

```bash
pytest tests/test_user_skills_storage.py -v
```

Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add NimoOS-AI/agent/user_skills/storage.py \
        NimoOS-AI/agent/tests/test_user_skills_storage.py
git commit -m "feat(user-skills): atomic CRUD with quota enforcement"
```

---

## Task 4: sweeper.py — 启动期清理

**Files:**
- Create: `NimoOS-AI/agent/user_skills/sweeper.py`
- Test: `NimoOS-AI/agent/tests/test_user_skills_sweeper.py`

- [ ] **Step 1: 写失败测试**

`agent/tests/test_user_skills_sweeper.py`：

```python
from user_skills.sweeper import sweep_root


def test_sweep_root_missing_returns_zero(tmp_path):
    assert sweep_root(tmp_path / "absent") == 0


def test_sweep_removes_tmp_and_old(tmp_path):
    user = tmp_path / "1"
    (user / ".tmp-abc").mkdir(parents=True)
    (user / ".old-xyz").mkdir(parents=True)
    (user / "real-skill").mkdir()
    (user / "real-skill" / "SKILL.md").write_text("---\nname: x\n---\n")
    (user / ".hidden").mkdir()  # not swept; not our prefix

    n = sweep_root(tmp_path)

    assert n == 2
    assert not (user / ".tmp-abc").exists()
    assert not (user / ".old-xyz").exists()
    assert (user / "real-skill").is_dir()
    assert (user / ".hidden").is_dir()


def test_sweep_handles_multiple_users(tmp_path):
    (tmp_path / "u1" / ".tmp-1").mkdir(parents=True)
    (tmp_path / "u2" / ".old-2").mkdir(parents=True)
    assert sweep_root(tmp_path) == 2
```

- [ ] **Step 2: 运行确认失败**

```bash
pytest tests/test_user_skills_sweeper.py -v
```

Expected: ImportError.

- [ ] **Step 3: 实现 sweeper.py**

`agent/user_skills/sweeper.py`：

```python
"""Startup cleanup of leftover .tmp-*/.old-* directories.

This is a single-process service; any such directory at startup is by
definition a leftover from a crashed write, so we sweep unconditionally.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

_PREFIXES = (".tmp-", ".old-")


def sweep_root(root: Path) -> int:
    root = Path(root)
    if not root.is_dir():
        return 0
    removed = 0
    for user_dir in root.iterdir():
        if not user_dir.is_dir():
            continue
        for d in user_dir.iterdir():
            if not d.is_dir():
                continue
            if any(d.name.startswith(pref) for pref in _PREFIXES):
                try:
                    shutil.rmtree(d)
                    removed += 1
                    log.info("swept stale skill artifact %s", d)
                except OSError as e:
                    log.warning("failed to sweep %s: %s", d, e)
    return removed
```

- [ ] **Step 4: 运行确认通过**

```bash
pytest tests/test_user_skills_sweeper.py -v
```

Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add NimoOS-AI/agent/user_skills/sweeper.py \
        NimoOS-AI/agent/tests/test_user_skills_sweeper.py
git commit -m "feat(user-skills): startup sweeper for stale tmp/old dirs"
```

---

## Task 5: context.py + tools.py — FunctionTool 包装

**Files:**
- Create: `NimoOS-AI/agent/user_skills/context.py`
- Create: `NimoOS-AI/agent/user_skills/tools.py`
- Test: `NimoOS-AI/agent/tests/test_user_skills_tools.py`

- [ ] **Step 1: 实现 context.py（无需独立测，简单常量）**

`agent/user_skills/context.py`：

```python
"""Context vars consumed by user_skills.tools.

USER_ID_VAR carries the current request's user_id; SKILLS_ROOT_VAR carries
the on-disk skills root (overridable in tests via ContextVar.set)."""
from __future__ import annotations

import os
from contextvars import ContextVar
from pathlib import Path

USER_ID_VAR: ContextVar[str] = ContextVar(
    "user_skill_user_id", default="",
)

_DEFAULT_ROOT = os.environ.get(
    "NIMOOS_AGENT_SKILLS_ROOT",
    str(Path.home() / ".nimoos" / "agent" / "user_skills"),
)
SKILLS_ROOT_VAR: ContextVar[str] = ContextVar(
    "user_skill_root", default=_DEFAULT_ROOT,
)


def current_root() -> Path:
    return Path(SKILLS_ROOT_VAR.get())
```

- [ ] **Step 2: 写 tools 失败测试**

`agent/tests/test_user_skills_tools.py`：

```python
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from user_skills import tools as t
from user_skills.context import SKILLS_ROOT_VAR, USER_ID_VAR
from user_skills import storage


def _md(name: str, desc: str = "Do a thing.", body: str = "Body.\n") -> str:
    return f"---\nname: {name}\ndescription: {desc}\n---\n{body}"


@pytest.fixture
def root(tmp_path, monkeypatch):
    SKILLS_ROOT_VAR.set(str(tmp_path))
    USER_ID_VAR.set("1")
    return tmp_path


@pytest.mark.asyncio
async def test_use_skill_returns_body(root):
    storage.create_skill(root, "1", "skill-a",
                         {"SKILL.md": _md("skill-a", body="The body.\n")})
    result = await t.use_skill.on_invoke_tool(
        MagicMock(), '{"name": "skill-a"}'
    )
    assert result == "The body.\n"


@pytest.mark.asyncio
async def test_use_skill_missing_returns_error(root):
    result = await t.use_skill.on_invoke_tool(
        MagicMock(), '{"name": "ghost"}'
    )
    assert result.startswith("Error:")
    assert "not found" in result


@pytest.mark.asyncio
async def test_use_skill_bad_name_returns_error(root):
    result = await t.use_skill.on_invoke_tool(
        MagicMock(), '{"name": "Bad/Name"}'
    )
    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_use_skill_no_user_returns_error(tmp_path):
    SKILLS_ROOT_VAR.set(str(tmp_path))
    USER_ID_VAR.set("")
    result = await t.use_skill.on_invoke_tool(
        MagicMock(), '{"name": "skill-a"}'
    )
    assert "no active user" in result


@pytest.mark.asyncio
async def test_read_resource_returns_text(root):
    storage.create_skill(root, "1", "skill-a", {
        "SKILL.md": _md("skill-a"),
        "scripts/hi.sh": "echo hello\n",
    })
    result = await t.read_skill_resource.on_invoke_tool(
        MagicMock(),
        '{"skill": "skill-a", "path": "scripts/hi.sh"}',
    )
    assert result == "echo hello\n"


@pytest.mark.asyncio
async def test_read_resource_path_traversal_blocked(root):
    storage.create_skill(root, "1", "skill-a", {"SKILL.md": _md("skill-a")})
    result = await t.read_skill_resource.on_invoke_tool(
        MagicMock(),
        '{"skill": "skill-a", "path": "../../etc/passwd"}',
    )
    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_read_resource_symlink_escape_blocked(root, tmp_path):
    storage.create_skill(root, "1", "skill-a", {
        "SKILL.md": _md("skill-a"),
        "scripts/ok.sh": "echo ok\n",
    })
    secret = tmp_path / "secret.txt"
    secret.write_text("classified")
    sd = root / "1" / "skill-a"
    (sd / "scripts" / "leak.sh").unlink(missing_ok=True)
    (sd / "scripts" / "leak.sh").symlink_to(secret)
    result = await t.read_skill_resource.on_invoke_tool(
        MagicMock(),
        '{"skill": "skill-a", "path": "scripts/leak.sh"}',
    )
    assert result.startswith("Error:")
    assert "classified" not in result


@pytest.mark.asyncio
async def test_read_resource_missing_file_returns_error(root):
    storage.create_skill(root, "1", "skill-a", {"SKILL.md": _md("skill-a")})
    result = await t.read_skill_resource.on_invoke_tool(
        MagicMock(),
        '{"skill": "skill-a", "path": "scripts/none.sh"}',
    )
    assert result.startswith("Error:")
    assert "not found" in result


@pytest.mark.asyncio
async def test_read_resource_offset_and_limit(root):
    body = "ABCDEFGHIJ" * 1000  # 10_000 bytes
    storage.create_skill(root, "1", "skill-a", {
        "SKILL.md": _md("skill-a"),
        "references/big.txt": body,
    })
    # First chunk
    r1 = await t.read_skill_resource.on_invoke_tool(
        MagicMock(),
        '{"skill": "skill-a", "path": "references/big.txt",'
        ' "offset": 0, "limit": 4096}',
    )
    assert r1.startswith("ABCDEFGHIJ")
    assert "[truncated, total=10000 bytes" in r1
    assert "offset=4096" in r1
    # Continuation
    r2 = await t.read_skill_resource.on_invoke_tool(
        MagicMock(),
        '{"skill": "skill-a", "path": "references/big.txt",'
        ' "offset": 4096, "limit": 4096}',
    )
    assert "[truncated" in r2
    # Tail (within remainder)
    r3 = await t.read_skill_resource.on_invoke_tool(
        MagicMock(),
        '{"skill": "skill-a", "path": "references/big.txt",'
        ' "offset": 8192, "limit": 4096}',
    )
    assert "[truncated" not in r3


@pytest.mark.asyncio
async def test_read_resource_binary_rejected(root):
    storage.create_skill(root, "1", "skill-a", {"SKILL.md": _md("skill-a")})
    # Sneak a binary by writing directly post-create.
    (root / "1" / "skill-a" / "scripts").mkdir(exist_ok=True)
    (root / "1" / "skill-a" / "scripts" / "blob.sh").write_bytes(
        b"\x00\x01\x02bad\x00"
    )
    result = await t.read_skill_resource.on_invoke_tool(
        MagicMock(),
        '{"skill": "skill-a", "path": "scripts/blob.sh"}',
    )
    assert result.startswith("Error:")
    assert "binary" in result
```

- [ ] **Step 3: 运行确认失败**

```bash
pytest tests/test_user_skills_tools.py -v
```

Expected: ImportError on `user_skills.tools`.

- [ ] **Step 4: 实现 tools.py**

`agent/user_skills/tools.py`：

```python
"""FunctionTool wrappers exposing user skills to the LLM."""
from __future__ import annotations

import logging

import yaml
from agents import function_tool

from .context import USER_ID_VAR, current_root
from .loader import read_skill_body
from .paths import (
    validate_resource_path, validate_skill_name, validate_user_id,
)

log = logging.getLogger(__name__)

RESOURCE_READ_MAX = 64 * 1024
_BINARY_PROBE_BYTES = 1024


def _active_user() -> str | None:
    uid = USER_ID_VAR.get()
    return uid or None


@function_tool
async def use_skill(name: str) -> str:
    """Load the full instructions for a user-defined skill by name. Call
    this when you've decided to apply one of the skills listed in
    <available-user-skills>. Returns the skill's SKILL.md body (without
    frontmatter). After reading, follow the instructions inside."""
    uid = _active_user()
    if not uid:
        return "Error: no active user; skills are unavailable."
    try:
        validate_user_id(uid)
        validate_skill_name(name)
    except ValueError as e:
        return f"Error: {e}"
    try:
        return read_skill_body(current_root(), uid, name)
    except FileNotFoundError:
        return f"Error: skill '{name}' not found (may have been deleted)."
    except IsADirectoryError:
        return "Error: invalid skill layout."
    except PermissionError:
        return "Error: permission denied reading skill."
    except UnicodeDecodeError:
        return "Error: SKILL.md is not valid UTF-8."
    except yaml.YAMLError:
        return "Error: SKILL.md frontmatter is not valid YAML."
    except ValueError as e:
        return f"Error: {e}"


def _looks_binary(blob: bytes) -> bool:
    return b"\x00" in blob[:_BINARY_PROBE_BYTES]


@function_tool
async def read_skill_resource(
    skill: str,
    path: str,
    offset: int = 0,
    limit: int = RESOURCE_READ_MAX,
) -> str:
    """Read a resource file bundled with a user skill. `skill` is the
    skill's name; `path` is a path relative to that skill's root
    (e.g. 'scripts/cleanup.sh', 'references/api.md'). Use this after
    use_skill() if the skill's body references a sibling file. Use
    `offset` (bytes) + `limit` (bytes, max 65536) to read large files
    in chunks; if the response ends with '[truncated, total=N bytes;
    call again with offset=M to continue]', call again with that offset
    to read the next chunk."""
    uid = _active_user()
    if not uid:
        return "Error: no active user; skills are unavailable."
    try:
        validate_user_id(uid)
        validate_skill_name(skill)
        path = validate_resource_path(path)
    except ValueError as e:
        return f"Error: {e}"
    if offset < 0:
        return "Error: offset must be >= 0"
    if limit <= 0 or limit > RESOURCE_READ_MAX:
        return f"Error: limit must be in (0, {RESOURCE_READ_MAX}]"
    skill_dir = current_root() / uid / skill
    try:
        target = (skill_dir / path).resolve(strict=True)
        base = skill_dir.resolve(strict=True)
        target.relative_to(base)
    except FileNotFoundError:
        return f"Error: file '{path}' not found in skill '{skill}'."
    except ValueError:
        return "Error: resource path escapes skill root."
    except OSError as e:
        return f"Error: {e}"
    if not target.is_file():
        return "Error: target is not a regular file."
    try:
        blob = target.read_bytes()
    except PermissionError:
        return "Error: permission denied."
    except OSError as e:
        return f"Error: {e}"
    if _looks_binary(blob):
        return "Error: binary files are not supported."
    total = len(blob)
    chunk = blob[offset : offset + limit]
    try:
        text = chunk.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return "Error: file is not valid UTF-8."
    if offset + limit < total:
        return text + (
            f"\n[truncated, total={total} bytes; "
            f"call again with offset={offset + limit} to continue]"
        )
    if offset >= total and total > 0:
        return f"[end of file, total={total} bytes]"
    return text
```

- [ ] **Step 5: 运行确认通过**

```bash
pytest tests/test_user_skills_tools.py -v
```

Expected: 全部 PASS。

- [ ] **Step 6: Commit**

```bash
git add NimoOS-AI/agent/user_skills/context.py \
        NimoOS-AI/agent/user_skills/tools.py \
        NimoOS-AI/agent/tests/test_user_skills_tools.py
git commit -m "feat(user-skills): use_skill + read_skill_resource tools"
```

---

## Task 6: agent.py 接线 — 提示词注入 + 工具列表 + ContextVar

**Files:**
- Modify: `NimoOS-AI/agent/agent.py`（`_compose_system_prompt` 签名 + body；`AgentRunner.run` 注入与 tools）
- Modify: `NimoOS-AI/agent/main.py`（启动 sweep + 把 `user_id` 透传调整）
- Test: `NimoOS-AI/agent/tests/test_user_skills_integration.py`

> 现状 `agent.py:58 _compose_system_prompt(conn, session_id, base, ...)` 不接受 user_id；本任务把 user_id 加到签名并在 `agent.py:221` 调用点传入。

- [ ] **Step 1: 写集成测试**

`agent/tests/test_user_skills_integration.py`：

```python
"""Integration: AgentRunner wires user skills into prompt + tools."""
from pathlib import Path
import pytest

from user_skills.context import SKILLS_ROOT_VAR
from user_skills import storage


def _md(name: str, desc: str = "Do a thing.", body: str = "Body.\n") -> str:
    return f"---\nname: {name}\ndescription: {desc}\n---\n{body}"


def test_compose_prompt_includes_skill_index(tmp_path):
    """When user has skills, _compose_system_prompt must include the
    <available-user-skills> block listing each skill's name+description."""
    SKILLS_ROOT_VAR.set(str(tmp_path))
    storage.create_skill(tmp_path, "42", "weekly-report",
                         {"SKILL.md": _md("weekly-report",
                                          desc="Generate weekly report.")})
    storage.create_skill(tmp_path, "42", "photo-album",
                         {"SKILL.md": _md("photo-album",
                                          desc="Build photo album.")})

    import db as db_module
    from agent import _compose_system_prompt
    conn = db_module.init_db(str(tmp_path / "agent.db"))
    # Need a session row so visible_resources query doesn't 500
    conn.execute(
        "INSERT INTO sessions (id, user_id, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?)", ("s1", "42", None, 0, 0),
    )

    prompt = _compose_system_prompt(conn, "s1", "42", "BASE")
    assert "<available-user-skills>" in prompt
    assert "weekly-report: Generate weekly report." in prompt
    assert "photo-album: Build photo album." in prompt
    assert "</available-user-skills>" in prompt


def test_compose_prompt_no_block_when_no_skills(tmp_path):
    SKILLS_ROOT_VAR.set(str(tmp_path))
    import db as db_module
    from agent import _compose_system_prompt
    conn = db_module.init_db(str(tmp_path / "agent.db"))
    conn.execute(
        "INSERT INTO sessions (id, user_id, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?)", ("s1", "42", None, 0, 0),
    )
    prompt = _compose_system_prompt(conn, "s1", "42", "BASE")
    assert "<available-user-skills>" not in prompt


def test_compose_prompt_isolates_users(tmp_path):
    SKILLS_ROOT_VAR.set(str(tmp_path))
    storage.create_skill(tmp_path, "1", "owned-by-1",
                         {"SKILL.md": _md("owned-by-1")})
    import db as db_module
    from agent import _compose_system_prompt
    conn = db_module.init_db(str(tmp_path / "agent.db"))
    conn.execute(
        "INSERT INTO sessions (id, user_id, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?)", ("s1", "2", None, 0, 0),
    )
    prompt = _compose_system_prompt(conn, "s1", "2", "BASE")
    assert "owned-by-1" not in prompt
```

- [ ] **Step 2: 运行确认失败**

```bash
pytest tests/test_user_skills_integration.py -v
```

Expected: 三个测试 fail（`_compose_system_prompt` 当前签名不收 user_id，且不读 skills）。

- [ ] **Step 3: 修改 `_compose_system_prompt` 签名 + body**

修改 `NimoOS-AI/agent/agent.py`。在文件顶部 imports 区域追加：

```python
from user_skills.context import USER_ID_VAR as SKILL_USER_ID_VAR, current_root as skill_root
from user_skills.loader import list_skill_index
from user_skills import tools as skill_tools
```

把 `_compose_system_prompt` 签名第三个位置参数从 `base` 改为 `user_id`，新增 `base`：

```python
def _compose_system_prompt(conn, session_id: str, user_id: str, base: str,
                            *, max_per_file: int = 8 * 1024,
                            max_total: int = 32 * 1024) -> str:
```

在函数末尾 `return base + block` 之前，把 `block` 末尾追加 skill 索引块：

```python
    # ---- existing visible_resources / agent.md handling ends here ----

    if user_id:
        try:
            entries = list_skill_index(skill_root(), user_id)
        except Exception as e:  # noqa: BLE001 — never block run on skills bug
            import logging
            logging.getLogger(__name__).warning(
                "list_skill_index failed for user %s: %s", user_id, e
            )
            entries = []
        if entries:
            lines = [
                "",
                "<available-user-skills>",
                "You have access to the following user-defined skills. "
                "Each skill is a specialized procedure with its own "
                "instructions. To USE a skill, call the `use_skill` tool "
                "with the skill's name to load its full instructions into "
                "your context. Do this when the user's request matches "
                "the skill's description.",
                "",
            ]
            for s in entries:
                lines.append(f"- {s.name}: {s.description}")
            lines.append("</available-user-skills>")
            block += "\n" + "\n".join(lines)

    return base + block
```

- [ ] **Step 4: 修改 `AgentRunner.run` 调用点**

在 `agent.py` 中：

(a) 找到 `agent.py:181-197` 的 ContextVar 注入区，在末尾添加：

```python
            SKILL_USER_ID_VAR.set(user_id)
```

(b) 找到 `agent.py:221`：

```python
            full_prompt = _compose_system_prompt(self._conn, session_id, base)
```

改为：

```python
            full_prompt = _compose_system_prompt(
                self._conn, session_id, user_id, base
            )
```

(c) 找到 `agent.py:227-233` 创建 `Agent` 的代码块：

```python
            agent = Agent(
                name="NimoOS Agent",
                instructions=full_prompt,
                tools=ALL_TOOLS,
                model=model,
                model_settings=model_settings,
            )
```

改为：

```python
            agent = Agent(
                name="NimoOS Agent",
                instructions=full_prompt,
                tools=ALL_TOOLS + [
                    skill_tools.use_skill,
                    skill_tools.read_skill_resource,
                ],
                model=model,
                model_settings=model_settings,
            )
```

- [ ] **Step 5: 运行集成测试确认通过**

```bash
pytest tests/test_user_skills_integration.py -v
```

Expected: 三个测试全部 PASS。

- [ ] **Step 6: 跑全量测试确认无回归**

```bash
pytest tests/ -v
```

Expected: 全绿。`test_agent.py` / `test_agent_init.py` 等老测试若调用 `_compose_system_prompt` 旧签名会报错——若发现，修这些测试调用以补上 `user_id`（用任意非空字符串如 `"1"` 或空串 `""` 表示无 skill 用户）。

- [ ] **Step 7: 在 `main.py` 启动期调 sweeper**

修改 `NimoOS-AI/agent/main.py`。在 `_runner = AgentRunner(_conn, confirm_mgr=_confirm_mgr)` 这一行（约 `main.py:32`）之前插入：

```python
from user_skills.sweeper import sweep_root
from user_skills.context import current_root as _skill_root
sweep_root(_skill_root())
```

- [ ] **Step 8: 启动一次本地服务确认不崩溃**

```bash
cd NimoOS-AI/agent && source venv/bin/activate
NIMOOS_AGENT_SKILLS_ROOT=/tmp/skills-smoke python -c "import main"
```

Expected: 模块导入成功，无异常（即使 `/tmp/skills-smoke` 不存在，sweep_root 应静默返回 0）。

- [ ] **Step 9: Commit**

```bash
git add NimoOS-AI/agent/agent.py NimoOS-AI/agent/main.py \
        NimoOS-AI/agent/tests/test_user_skills_integration.py
git commit -m "feat(user-skills): wire skills into AgentRunner + system prompt"
```

---

## Task 7: 手动端到端冒烟（不写为自动测试）

**Files:** 仅 shell。

> 这个任务用于在本地确认从 "用户落盘 skill 文件 → 下次发消息看到索引" 的真实链路。不写自动化测试，因为它需要真实 LLM 调用；保留为开发期手动检查清单。

- [ ] **Step 1: 准备一个最小 skill 在磁盘上**

```bash
SKILLS_ROOT="$HOME/.nimoos/agent/user_skills"
mkdir -p "$SKILLS_ROOT/1/hello-skill"
cat > "$SKILLS_ROOT/1/hello-skill/SKILL.md" <<'MD'
---
name: hello-skill
description: Greets the user with their name and the current weather city.
---
When invoked, ask the user for their city, then say:
"Hello, friend in <city>!"
MD
```

- [ ] **Step 2: 启动服务**

```bash
cd NimoOS-AI/agent && source venv/bin/activate && python main.py &
sleep 2
```

- [ ] **Step 3: 创建 session 并发一条消息**

```bash
SESSION=$(curl -sf -X POST http://127.0.0.1:8282/agent/sessions \
    -H "X-User-Id: 1" | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "session=$SESSION"

# 发一条会触发 hello-skill 的消息
curl -N -X POST http://127.0.0.1:8282/agent/chat \
    -H "X-User-Id: 1" -H "Content-Type: application/json" \
    -d "{\"session_id\":\"$SESSION\",\"message\":\"please greet me\"}"
```

Expected: SSE 流里出现 `tool_call=use_skill` 与 `tool_result`（正文文本），随后是模型按 SKILL.md 指引的回复。如果 LLM 没主动调 `use_skill`（取决于具体 model），最低标准是看到模型的 system prompt 里包含 `<available-user-skills>` 块——在 server 日志里 grep 一下。

- [ ] **Step 4: 确认进程关停**

```bash
pkill -f "python main.py"
```

- [ ] **Step 5: 不需要 commit**（无文件改动）。

---

## Self-Review

**Spec coverage**（对照 `2026-04-29-markdown-skills-design.md`）：

| 设计节 | 落入任务 |
|---|---|
| §3 SKILL.md frontmatter 形态 | Task 2 (`parse_frontmatter`) |
| §4 存储布局 + `.tmp-*` 跳过 + 写入崩溃清理 | Task 3 (`_atomic_write_skill`) + Task 4 (`sweep_root`) |
| §4 description 单行 / 拒绝 `<>` / 控制字符 | Task 1 (`validate_description`) |
| §4 配额（50 skill / 1 MiB / 100 files / 256 KiB / 32 KiB / 256 char）| Task 1 常量 + Task 3 校验 |
| §5.1 系统提示词索引块 | Task 6 (`_compose_system_prompt`) |
| §5.2 `use_skill` + 异常字符串化 | Task 5 (`use_skill`) |
| §5.3 `read_skill_resource` + offset/limit + symlink 防逃逸 | Task 5 (`read_skill_resource`) |
| §6 工具集组装 | Task 6 步骤 4(c) |
| §7 提示词注入位置 | Task 6 步骤 3 |
| §9 即时生效（每次 run 扫盘） | Task 2 + Task 6 自然达成（无缓存）|
| §10 安全表所有行 | Task 1（清洗）+ Task 3（原子 + 配额）+ Task 4（sweep）+ Task 5（异常 / symlink / 二进制 / UTF-8 / offset）|

**未覆盖（明确不在本计划）**：HTTP API（步骤 3）、前端 UI（步骤 4），按设计文档独立成 plan。

**Placeholder scan**：无 "TBD" / "适当处理" / "类似 Task N" 之类占位；每个 Step 都给了完整代码或命令。

**Type consistency**：`SkillEntry`、`USER_ID_VAR`、`SKILLS_ROOT_VAR`、`current_root`、`list_skill_index`、`read_skill_body`、`use_skill`、`read_skill_resource`、`_atomic_write_skill`、`sweep_root`、`validate_skill_name` / `validate_user_id` / `validate_description` / `validate_resource_path` 在所有任务里命名一致；`_compose_system_prompt` 新签名为 `(conn, session_id, user_id, base, ...)`，集成测、调用点、实现三处一致。
