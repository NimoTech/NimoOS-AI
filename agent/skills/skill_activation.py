"""Server-side skill auto-activation (spec 2026-09-08-skill-auto-activation).

A skill manifest may carry
    "activation": {"keywords": ["哪些", "list all", ...], "first_tool": "nimoos_search"}
select_auto_skill() decides from the user's message alone whether exactly one
skill in the runtime view is force-loaded this turn. Pure string matching: no
regex is compiled from manifest data (phrases go through re.escape, so the
pattern is literal and linear), no I/O, no model call. Every entry point
swallows its own exceptions — prompt composition must never fail because a
manifest is odd.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass

_log = logging.getLogger(__name__)

# Mirrors service/skills_store.go ActivationFirstTools. Read-only core tools.
ALLOWED_FIRST_TOOLS: frozenset[str] = frozenset({"nimoos_search", "read_document", "read_file_chunk"})

MIN_MESSAGE_CHARS = 6
MAX_KEYWORDS = 64
INJECT_CAP_BYTES = 16 * 1024

# Provider types whose chat-completions endpoint honours a named tool_choice
# with stream=true. Set by the probe task (plan Task 5); types outside it get
# the prompt injection only. Values are ProviderType strings.
FORCE_PROVIDER_TYPES: frozenset[str] = frozenset({"openai", "deepseek", "qwen", "other"})

# NAS-operations vocabulary. A hit means the question is about the box, not
# about the user's documents, so no document skill is activated.
NEGATIVE_PHRASES: tuple[str, ...] = (
    "docker", "容器", "compose", "应用", "安装", "卸载", "重启", "磁盘", "硬盘",
    "raid", "挂载", "systemd", "端口", "密码", "权限", "日志",
    "app", "apps", "container", "install", "uninstall", "restart", "disk",
    "mount", "port", "password", "permission", "log",
)

_LATIN_RE = re.compile(r"^[\x00-\x7f]+$")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class ActivatedSkill:
    skill_id: str
    hits: int
    first_tool: str | None


def normalize(text: str) -> str:
    """NFKC, casefold, single spaces, trimmed."""
    t = unicodedata.normalize("NFKC", text or "").casefold()
    return _WS_RE.sub(" ", t).strip()


def _phrase_matches(norm_text: str, phrase: str) -> bool:
    p = normalize(phrase)
    if not p:
        return False
    if _LATIN_RE.match(p):
        # Whole-word for Latin phrases: "compare" must not fire on "compared".
        # re.escape makes the pattern literal, so this cannot be a ReDoS vector.
        return re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", norm_text) is not None
    return p in norm_text


def match_keywords(text: str, keywords) -> int:
    """Number of DISTINCT keywords (first MAX_KEYWORDS only) found in text."""
    norm = normalize(text)
    hits = 0
    for k in list(keywords or [])[:MAX_KEYWORDS]:
        if isinstance(k, str) and _phrase_matches(norm, k):
            hits += 1
    return hits


def has_negative(text: str) -> bool:
    norm = normalize(text)
    return any(_phrase_matches(norm, p) for p in NEGATIVE_PHRASES)


def select_auto_skill(message: str, skills) -> ActivatedSkill | None:
    """Pick at most one skill for this message.

    `skills` is the list returned by skills_registry._scan_runtime_view().
    Rules: message >= MIN_MESSAGE_CHARS after normalization, not a slash
    command, no NAS-operations phrase; among skills with >= 1 keyword hit,
    most distinct hits wins, ties by skill_id order; manual-trigger skills
    and skills without an activation block never match.
    """
    try:
        norm = normalize(message)
        if len(norm) < MIN_MESSAGE_CHARS or norm.startswith("/"):
            return None
        if has_negative(message):
            return None
        best: ActivatedSkill | None = None
        for s in sorted(skills or [], key=lambda x: str(x.get("skill_id", ""))):
            if s.get("trigger") == "manual":
                continue
            act = s.get("activation")
            if not isinstance(act, dict):
                continue
            hits = match_keywords(message, act.get("keywords"))
            if hits == 0:
                continue
            ft = act.get("first_tool")
            ft = ft if isinstance(ft, str) and ft in ALLOWED_FIRST_TOOLS else None
            cand = ActivatedSkill(str(s.get("skill_id", "")), hits, ft)
            if best is None or cand.hits > best.hits:
                best = cand
        return best
    except Exception:
        _log.warning("select_auto_skill failed", exc_info=True)
        return None


def forcing_enabled() -> bool:
    """Process-wide kill switch for the forced first tool call."""
    return os.environ.get("NIMOOS_SKILL_FORCE_FIRST_TOOL", "1") != "0"


def render_activation_block(skill_id: str, skill_md: str) -> str:
    return (
        f'<activated-skill id="{skill_id}" mode="auto">\n'
        "[Activated automatically because the question looks like it should be "
        "answered from the user's own documents. If it is actually about NAS "
        "operations, code or general knowledge, ignore this block and answer "
        "normally.]\n\n"
        f"{skill_md.strip()}\n"
        "</activated-skill>"
    )
