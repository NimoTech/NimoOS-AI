import json
import os
from pathlib import Path

import pytest

from skills import skill_activation as sa
from skills.skills_registry import SKILLS_ROOT_VAR, USER_ID_VAR, _scan_runtime_view

# agent/tests/ -> agent/ -> repo root -> builtin-skills/
_MANIFEST = Path(__file__).resolve().parents[2] / "builtin-skills" / "deep-search" / "manifest.json"


def _deep_search_entry() -> dict:
    m = json.loads(_MANIFEST.read_text())
    return {"id": m["id"], "name": m["name"], "description": m["description"],
            "trigger": m["trigger"], "skill_id": m["id"], "activation": m["activation"]}


PROBE_QUESTIONS = [
    "酷睿i3系列中哪些是12th处理器？全部列出来 从小到大排序，只用列出型号即可",
    "资料库中哪些是Lunar Lake的处理器？从小到大排序",
    "xeon e3系列中，TDP 最低的是哪一款？",
    "xeon-processor-e系列中，首个支持 DDR4 内存（按发布日期最早）的型号是哪一款？",
    "xeon-processor-e1225系列中，哪一款的最大内存带宽最高？",
]


@pytest.mark.parametrize("q", PROBE_QUESTIONS)
def test_probe_questions_activate_deep_search(q):
    got = sa.select_auto_skill(q, [_deep_search_entry()])
    assert got is not None and got.skill_id == "deep-search"
    assert got.first_tool == "nimoos_search"


def test_manifest_examples_activate_deep_search():
    m = json.loads(_MANIFEST.read_text())
    for ex in m["examples"]:
        assert sa.select_auto_skill(ex, [_deep_search_entry()]) is not None, ex


@pytest.mark.parametrize("q", [
    "哪个 docker 应用最占内存",                  # NAS operations, Chinese
    "restart the plex container",                # NAS operations, English
    "which disk is the largest",                 # NAS operations via negative guard
    "你好",                                      # greeting, under 6 chars
    "帮我写个快排",                              # coding, no keyword
    "/deep-search 哪些处理器是 12 代",          # slash path is not ours
    "最高",                                      # keyword alone, too short
])
def test_non_document_questions_do_not_activate(q):
    assert sa.select_auto_skill(q, [_deep_search_entry()]) is None


def test_latin_phrases_match_whole_words_only():
    assert sa.match_keywords("I compared them yesterday", ["compare"]) == 0
    assert sa.match_keywords("please compare A and B", ["compare"]) == 1
    assert sa.match_keywords("Compare  A   and B", ["compare"]) == 1   # casefold + whitespace


def test_cjk_phrases_match_as_substrings():
    assert sa.match_keywords("这两款有什么区别呢", ["区别"]) == 1


def test_counts_distinct_keywords_once_each():
    assert sa.match_keywords("哪些 哪些 哪些 最高", ["哪些", "最高", "最低"]) == 2


def _entry(sid: str, keywords, first_tool="nimoos_search", trigger="auto") -> dict:
    return {"id": sid, "name": sid, "description": "", "trigger": trigger, "skill_id": sid,
            "activation": {"keywords": keywords, "first_tool": first_tool}}


def test_picks_the_skill_with_most_hits_and_only_one():
    skills = [_entry("alpha", ["哪些"]), _entry("beta", ["哪些", "排序"])]
    got = sa.select_auto_skill("哪些型号，按大小排序", skills)
    assert got is not None and got.skill_id == "beta" and got.hits == 2


def test_tie_breaks_by_skill_id_order():
    skills = [_entry("zeta", ["哪些"]), _entry("alpha", ["哪些"])]
    got = sa.select_auto_skill("哪些型号支持这个功能", skills)
    assert got is not None and got.skill_id == "alpha"


def test_manual_trigger_and_missing_activation_are_skipped():
    skills = [_entry("m", ["哪些"], trigger="manual"),
              {"id": "n", "name": "n", "description": "", "trigger": "auto", "skill_id": "n"}]
    assert sa.select_auto_skill("哪些型号支持这个功能", skills) is None


def test_disallowed_first_tool_is_dropped_not_fatal():
    got = sa.select_auto_skill("哪些型号支持这个功能", [_entry("x", ["哪些"], first_tool="run_command")])
    assert got is not None and got.first_tool is None


def test_keyword_list_is_capped_at_64():
    kws = [f"kw{i:03d}" for i in range(70)]
    assert sa.match_keywords("kw069 is here", kws) == 0
    assert sa.match_keywords("kw063 is here", kws) == 1


def test_forcing_enabled_env(monkeypatch):
    monkeypatch.delenv("NIMOOS_SKILL_FORCE_FIRST_TOOL", raising=False)
    assert sa.forcing_enabled() is True
    monkeypatch.setenv("NIMOOS_SKILL_FORCE_FIRST_TOOL", "0")
    assert sa.forcing_enabled() is False


def test_render_activation_block_shape():
    block = sa.render_activation_block("deep-search", "## Deep search\nbody\n")
    assert block.startswith('<activated-skill id="deep-search" mode="auto">')
    assert block.rstrip().endswith("</activated-skill>")
    assert "ignore this block" in block
    assert "## Deep search\nbody" in block


def test_scan_runtime_view_exposes_activation(tmp_path):
    rt = tmp_path / ".runtime" / "42"
    rt.mkdir(parents=True)
    builtin = tmp_path / "builtin"
    d = builtin / "alpha"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "id": "alpha", "name": "alpha", "title": "alpha",
        "trigger": "auto", "color": "blue", "icon": "sparkle", "description": "d",
        "version": "0.1.0", "author": "Test", "examples": [],
        "activation": {"keywords": ["哪些"], "first_tool": "nimoos_search"},
    }))
    (d / "SKILL.md").write_text("## alpha")
    os.symlink(d, rt / "alpha")
    SKILLS_ROOT_VAR.set(str(tmp_path))
    USER_ID_VAR.set("42")
    skills = _scan_runtime_view()
    assert skills[0]["activation"] == {"keywords": ["哪些"], "first_tool": "nimoos_search"}
