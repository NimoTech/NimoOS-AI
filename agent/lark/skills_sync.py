"""Convert lark-cli's embedded skills into NimoOS user skills.

Registers a fixed whitelist of the CLI's bundled skills as per-user NimoOS
skills through Task 7's internal Go endpoint
(``POST /v1/ai/_internal/skills/install`` / ``.../remove``), so a bound
Feishu account gets working-out-of-the-box `lark-base` / `lark-doc` /
`lark-im` / `lark-drive` skills without the user hand-writing SKILL.md files.

RECORDED, NOT GUESSED (probed against the real lark-cli v1.0.85 on 118 with a
throwaway HOME -- same rig as `lark/binding.py`'s module docstring, never the
real `~/.lark-cli`):

* `lark-cli skills list` writes its JSON envelope
  (`{"ok": true, "skills": [{"name", "description", "version", ...}], ...}`)
  to **stdout**; stderr is empty. This is the *opposite* of `auth`/`config`
  commands (see `binding.py`), which live entirely on stderr.
* `lark-cli skills read <name>` writes the raw SKILL.md (YAML frontmatter +
  markdown body, verbatim) to **stdout**; stderr carries only a human "Tip:
  read this skill's own files with ..." line (plus, when a newer CLI version
  is available, an update-notice blob) -- never JSON, never needed here.
* Neither subcommand needs `config show` / `auth login` to have completed:
  the content is embedded in the CLI binary at build time and is served
  independent of Feishu app config or login state. `sync()` therefore does
  not gate on `binding.status()` -- it is only ever invoked (by the bound
  hook in `binding.py`) once binding has actually succeeded, but it does not
  itself require it.
* An unknown/misspelled skill name is a normal CLI error: exit 2, stderr JSON
  `{"ok": false, "error": {...}}`.

Subprocess plumbing (env allowlist, per-user HOME, JSON-with-preamble
parsing) is reused from `binding.py` rather than re-implemented, since the
behaviour (and its tests) already live there.
"""

from __future__ import annotations

import logging
import re

import httpx

from channels.credentials import _internal_token
from lark import binding as _binding
from mcp_client.runtime import _read_ai_base

_LOG = logging.getLogger("nimoos-agent.lark")

# First batch: only these four embedded skills are converted. Anything else
# `skills list` reports is ignored.
WHITELIST = ("lark-base", "lark-doc", "lark-im", "lark-drive")

INSTALL_PATH = "/v1/ai/_internal/skills/install"
REMOVE_PATH = "/v1/ai/_internal/skills/remove"

HTTP_TIMEOUT = 10.0
LIST_TIMEOUT = 15.0
READ_TIMEOUT = 15.0

# Go's sanitizeSkillDescription (route/v2/skills.go) also folds newlines,
# drops control chars, replaces <>, and caps at 256 runes. This is a first
# pass so the request body is already well-formed -- double insurance, not a
# substitute.
MAX_DESCRIPTION_RUNES = 250

# service.MaxSkillMDBytes (skills_store.go) is the Go-side hard cap; going
# over it makes InstallInternal fail outright. Truncate a bit under it so the
# NimoOS note + truncation notice we add never themselves push it back over.
GO_MAX_MD_BYTES = 50 * 1024
MD_TRUNCATE_TO_BYTES = 49 * 1024

NOTE = (
    "> NimoOS note: run `lark-cli` commands in the shell (it is on PATH).\n"
    "> Reference files mentioned below are NOT bundled; read them at runtime with\n"
    "> `lark-cli skills read <skill>/<path>` in the shell.\n\n"
)

TRUNCATION_NOTICE = "\n\n> [NimoOS note: SKILL.md truncated to fit the 50 KiB limit]\n"

_WS_RE = re.compile(r"[ \t]+")


def _oneline_description(text: str) -> str:
    """Fold to a single line, cap at MAX_DESCRIPTION_RUNES + an ellipsis.

    Newlines become spaces and `<`/`>` become `(`/`)` (mirrors the Go-side
    sanitizer, applied here too so a request never depends on the Go pass to
    become well-formed). Only truncated text gets the trailing "…" marker.
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", " ").replace("<", "(").replace(">", ")")
    text = _WS_RE.sub(" ", text).strip()
    runes = list(text)
    if len(runes) > MAX_DESCRIPTION_RUNES:
        text = "".join(runes[:MAX_DESCRIPTION_RUNES]) + "…"
    if not text:
        # Defensive only: every official description is long and non-empty.
        # Go 400s an empty description, so this must never come out blank.
        text = "lark skill"
    return text


def _prepare_md(original: str) -> str:
    """Prepend the NimoOS note, then truncate if the result is too big."""
    md = NOTE + (original or "")
    raw = md.encode("utf-8")
    if len(raw) > GO_MAX_MD_BYTES:
        truncated = raw[:MD_TRUNCATE_TO_BYTES]
        # utf-8 decode with 'ignore' drops any partial trailing sequence
        # rather than raising on a byte-cut multi-byte character.
        md = truncated.decode("utf-8", errors="ignore") + TRUNCATION_NOTICE
    return md


def _build_manifest(name: str, description: str, md: str) -> dict:
    return {
        "name": name,
        "title": name,
        "description": _oneline_description(description),
        "trigger": "auto",
        "color": "blue",
        "icon": "grid",
        "md": _prepare_md(md),
        "examples": [],
    }


async def _list_skills(uid: str) -> list[dict]:
    """`lark-cli skills list`, filtered down to the whitelisted subset.

    Reads whichever stream actually has content (stdout first, per the
    recorded behaviour) so a future CLI build that flips streams -- as
    `auth`/`config` already do relative to `skills` -- degrades to "nothing
    found" instead of raising.
    """
    rc, out, err = await _binding._run(uid, ["skills", "list"], LIST_TIMEOUT)
    if rc != 0:
        _LOG.warning("lark-cli skills list failed for uid=%s rc=%d err=%s", uid, rc, err[:200])
        return []
    doc = _binding._parse_json(out) or _binding._parse_json(err)
    if not isinstance(doc, dict):
        return []
    skills = doc.get("skills")
    if not isinstance(skills, list):
        return []
    return [s for s in skills if isinstance(s, dict) and s.get("name") in WHITELIST]


async def _read_skill_md(uid: str, name: str) -> str | None:
    """`lark-cli skills read <name>` -> raw SKILL.md text, or None on failure."""
    rc, out, err = await _binding._run(uid, ["skills", "read", name], READ_TIMEOUT)
    if rc != 0:
        _LOG.warning(
            "lark-cli skills read %s failed for uid=%s rc=%d err=%s", name, uid, rc, err[:200]
        )
        return None
    return out


def _internal_headers(uid: str, action: str) -> dict:
    """X-Internal-Token header for the Go internal endpoints, read the same
    way `channels/credentials.py` does. Both install/remove now require it
    (route/v2.go wires v2.InternalTokenOnly onto them) since user_id here
    comes from the request body, not a JWT -- LocalhostOnly alone doesn't
    stop the agent's own sandboxed processes, which share loopback via
    network_mode: host. A missing token isn't fatal here: the request still
    goes out and degrades to the existing HTTP-failure path (401, caught and
    logged by the caller) rather than raising a different error shape.
    """
    token = _internal_token()
    if not token:
        _LOG.warning(
            "lark skill %s: internal token unreadable, request will be rejected (uid=%s)",
            action, uid,
        )
        return {}
    return {"X-Internal-Token": token}


async def _post_install(base: str, uid: str, skill: dict) -> dict:
    url = base.rstrip("/") + INSTALL_PATH
    headers = _internal_headers(uid, "install")
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(url, json={"user_id": uid, "skill": skill}, headers=headers)
    if resp.status_code >= 400:
        raise RuntimeError(
            f"install {skill.get('name')!r} failed: HTTP {resp.status_code} {resp.text[:200]}"
        )
    return resp.json()


async def _post_remove(base: str, uid: str, skill_id: str) -> None:
    url = base.rstrip("/") + REMOVE_PATH
    headers = _internal_headers(uid, "remove")
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(url, json={"user_id": uid, "id": skill_id}, headers=headers)
    if resp.status_code >= 400:
        raise RuntimeError(
            f"remove {skill_id!r} failed: HTTP {resp.status_code} {resp.text[:200]}"
        )


async def sync(uid: str) -> dict:
    """Convert + register the whitelisted embedded lark skills for `uid`.

    Returns `{"installed": [<id>, ...], "failed": [{"name", "error"}, ...]}`.
    A single skill's read/HTTP failure is isolated (recorded in `failed`) and
    never stops the rest of the whitelist from being processed.
    """
    installed: list[str] = []
    failed: list[dict] = []

    base = _read_ai_base()
    if not base:
        return {
            "installed": installed,
            "failed": [{"name": n, "error": "ai.url is unreadable"} for n in WHITELIST],
        }

    for skill in await _list_skills(uid):
        name = skill.get("name")
        try:
            md = await _read_skill_md(uid, name)
            if md is None:
                failed.append({"name": name, "error": "skills read failed"})
                continue
            manifest = _build_manifest(name, skill.get("description", ""), md)
            result = await _post_install(base, uid, manifest)
            installed.append(result.get("id") or name)
        except Exception as exc:  # noqa: BLE001 - isolate one skill's failure
            _LOG.warning("lark skill sync failed for uid=%s name=%s: %s", uid, name, exc)
            failed.append({"name": name, "error": str(exc)})

    return {"installed": installed, "failed": failed}


async def remove_all(uid: str) -> None:
    """Remove every whitelisted skill id for `uid`. Best-effort, per id.

    Called from `binding.unbind()`; a failure here must never block or fail
    the unbind itself, so each id is isolated and only logged on error. Does
    not consult `lark-cli` at all -- the ids are exactly `WHITELIST` (name ==
    slug for all four), so this works even when the CLI/session is already
    gone by the time DELETE runs.
    """
    base = _read_ai_base()
    if not base:
        _LOG.warning("lark skills remove_all: ai.url is unreadable, skipping for uid=%s", uid)
        return
    for skill_id in WHITELIST:
        try:
            await _post_remove(base, uid, skill_id)
        except Exception:  # noqa: BLE001 - best-effort, one id must not block others
            _LOG.warning("lark skill remove failed for uid=%s id=%s", uid, skill_id, exc_info=True)
