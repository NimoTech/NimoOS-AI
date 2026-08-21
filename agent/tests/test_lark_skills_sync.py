"""Tests for converting lark-cli's embedded skills into NimoOS user skills.

The fake CLI below is a bash script that reproduces the *recorded* behaviour
of the real lark-cli v1.0.85's `skills` subcommand, probed against the real
binary with a throwaway HOME (never the real `~/.lark-cli`):

  * `skills list` writes its JSON envelope to **stdout**; stderr is empty --
    the opposite of `auth`/`config` (see `tests/test_lark_binding.py`).
  * `skills read <name>` writes the raw SKILL.md to **stdout**; stderr only
    ever carries a human tip line, never JSON.
  * Neither subcommand needs `config show`/`auth login` to have run first --
    confirmed by never creating `$HOME/.lark-cli` in this fixture at all.
  * An unknown/misspelled skill name exits non-zero with a stderr JSON error
    envelope -- reproduced here for `lark-im` to exercise "one skill's read
    failure must not block the rest of the whitelist".
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

from lark import skills_sync

UID = "u9"

BASE_DESC = "Base ops.\nSecond line <tag>"
DOC_DESC = "文" * 300  # exercises rune-based (not byte-based) truncation
IM_DESC = "IM ops"
DRIVE_DESC = "Drive ops"

BASE_MD = "## Lark Base\n\nDo base stuff.\n"
DOC_MD = "## Lark Doc\n\nDo doc stuff.\n"

LIST_JSON = json.dumps(
    {
        "ok": True,
        "skills": [
            {"name": "lark-base", "description": BASE_DESC, "version": "1.0.0"},
            {"name": "lark-doc", "description": DOC_DESC, "version": "1.0.0"},
            {"name": "lark-im", "description": IM_DESC, "version": "1.0.0"},
            {"name": "lark-drive", "description": DRIVE_DESC, "version": "1.0.0"},
            {"name": "lark-not-whitelisted", "description": "skip me", "version": "1.0.0"},
        ],
        "count": 5,
    },
    ensure_ascii=False,
)

# Fake lark-cli. `skills list` -> stdout JSON (real behaviour: stdout, NOT
# stderr, unlike auth/config). `skills read <name>` -> stdout raw md, except
# lark-im (simulates a read failure) and lark-drive (oversized, to exercise
# the 50 KiB truncation path).
FAKE_CLI_TEMPLATE = r"""#!/bin/bash
case "$1 $2" in
  "skills list")
    cat <<'__EOF_LIST__'
__LIST_JSON__
__EOF_LIST__
    ;;
  "skills read")
    case "$3" in
      lark-base)
        cat <<'__EOF_BASE__'
__BASE_MD__
__EOF_BASE__
        ;;
      lark-doc)
        cat <<'__EOF_DOC__'
__DOC_MD__
__EOF_DOC__
        ;;
      lark-im)
        echo '{"ok":false,"error":{"type":"validation","message":"boom"}}' >&2
        exit 2
        ;;
      lark-drive)
        head -c 60000 /dev/zero | tr '\0' 'y'
        ;;
      *)
        echo '{"ok":false,"error":{"type":"validation"}}' >&2
        exit 2
        ;;
    esac
    ;;
  *)
    echo '{"ok":false,"error":{"type":"validation"}}' >&2
    exit 2
    ;;
esac
"""


@pytest.fixture
def lark_cli_env(tmp_path, monkeypatch):
    """Install the fake CLI + a throwaway HOMES_ROOT (mirrors test_lark_binding.py)."""
    from skills import shell

    homes = tmp_path / "homes"
    homes.mkdir()
    monkeypatch.setattr(shell, "HOMES_ROOT", homes)

    script = (
        FAKE_CLI_TEMPLATE.replace("__LIST_JSON__", LIST_JSON)
        .replace("__BASE_MD__", BASE_MD)
        .replace("__DOC_MD__", DOC_MD)
    )
    fake = tmp_path / "lark-cli"
    fake.write_text(script)
    fake.chmod(0o755)
    monkeypatch.setenv("NIMOOS_LARK_CLI", str(fake))
    return {"homes": homes, "bin": fake}


def run_async(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------


def test_oneline_description_folds_newlines_and_tags():
    assert skills_sync._oneline_description("a\nb <x>") == "a b (x)"


def test_oneline_description_truncates_by_rune_with_ellipsis():
    out = skills_sync._oneline_description("z" * 400)
    assert out == "z" * 250 + "…"
    assert len(out) == 251


def test_oneline_description_short_text_unchanged_no_ellipsis():
    assert skills_sync._oneline_description("short desc") == "short desc"


def test_prepare_md_prepends_note_when_small():
    out = skills_sync._prepare_md("hello")
    assert out == skills_sync.NOTE + "hello"


def test_prepare_md_truncates_when_oversized():
    big = "a" * (skills_sync.GO_MAX_MD_BYTES + 5000)
    out = skills_sync._prepare_md(big)
    assert len(out.encode("utf-8")) <= skills_sync.GO_MAX_MD_BYTES
    assert out.endswith(skills_sync.TRUNCATION_NOTICE)
    assert out.startswith(skills_sync.NOTE)


# --------------------------------------------------------------------------
# sync(uid)
# --------------------------------------------------------------------------


def test_sync_filters_whitelist_converts_and_isolates_read_failure(lark_cli_env, monkeypatch):
    monkeypatch.setattr(skills_sync, "_read_ai_base", lambda: "http://127.0.0.1:9999")

    install_calls = []

    async def fake_post_install(base, uid, skill):
        install_calls.append((uid, dict(skill)))
        return {"id": skill["name"]}

    monkeypatch.setattr(skills_sync, "_post_install", fake_post_install)

    result = run_async(skills_sync.sync(UID))

    attempted = {c[1]["name"] for c in install_calls}
    assert attempted == {"lark-base", "lark-doc", "lark-drive"}, (
        "lark-not-whitelisted must never be considered; lark-im's read "
        "fails before install is ever attempted"
    )

    assert set(result["installed"]) == {"lark-base", "lark-doc", "lark-drive"}
    assert len(result["failed"]) == 1
    assert result["failed"][0]["name"] == "lark-im"

    by_name = {c[1]["name"]: c[1] for c in install_calls}

    base_skill = by_name["lark-base"]
    assert base_skill["title"] == "lark-base"
    assert base_skill["trigger"] == "auto"
    assert base_skill["color"] == "blue"
    assert base_skill["icon"] == "grid"
    assert base_skill["examples"] == []
    assert base_skill["description"] == "Base ops. Second line (tag)"
    assert len(base_skill["description"]) <= 256
    assert base_skill["md"].startswith(skills_sync.NOTE)
    assert "Do base stuff." in base_skill["md"]

    doc_skill = by_name["lark-doc"]
    assert doc_skill["description"] == "文" * 250 + "…"
    assert len(doc_skill["description"]) <= 256

    drive_skill = by_name["lark-drive"]
    md_bytes = drive_skill["md"].encode("utf-8")
    assert len(md_bytes) <= skills_sync.GO_MAX_MD_BYTES
    assert "truncated" in drive_skill["md"]
    assert drive_skill["md"].startswith(skills_sync.NOTE)


def test_sync_isolates_single_install_failure(lark_cli_env, monkeypatch):
    monkeypatch.setattr(skills_sync, "_read_ai_base", lambda: "http://127.0.0.1:9999")

    async def flaky_post_install(base, uid, skill):
        if skill["name"] == "lark-base":
            raise RuntimeError("boom install")
        return {"id": skill["name"]}

    monkeypatch.setattr(skills_sync, "_post_install", flaky_post_install)

    result = run_async(skills_sync.sync(UID))

    assert set(result["installed"]) == {"lark-doc", "lark-drive"}
    failed_names = {f["name"] for f in result["failed"]}
    assert failed_names == {"lark-base", "lark-im"}


def test_sync_without_ai_base_fails_all_without_touching_cli(monkeypatch):
    monkeypatch.setattr(skills_sync, "_read_ai_base", lambda: None)

    async def boom_list(uid):
        raise AssertionError("must not call lark-cli when ai.url is unreadable")

    monkeypatch.setattr(skills_sync, "_list_skills", boom_list)

    result = run_async(skills_sync.sync(UID))
    assert result["installed"] == []
    assert {f["name"] for f in result["failed"]} == set(skills_sync.WHITELIST)


# --------------------------------------------------------------------------
# remove_all(uid)
# --------------------------------------------------------------------------


def test_remove_all_calls_every_whitelist_id(monkeypatch):
    monkeypatch.setattr(skills_sync, "_read_ai_base", lambda: "http://127.0.0.1:9999")
    calls = []

    async def fake_post_remove(base, uid, skill_id):
        calls.append((uid, skill_id))

    monkeypatch.setattr(skills_sync, "_post_remove", fake_post_remove)

    run_async(skills_sync.remove_all(UID))

    assert calls == [(UID, sid) for sid in skills_sync.WHITELIST]


def test_remove_all_isolates_single_failure(monkeypatch):
    monkeypatch.setattr(skills_sync, "_read_ai_base", lambda: "http://127.0.0.1:9999")
    calls = []

    async def flaky_post_remove(base, uid, skill_id):
        calls.append(skill_id)
        if skill_id == "lark-doc":
            raise RuntimeError("boom remove")

    monkeypatch.setattr(skills_sync, "_post_remove", flaky_post_remove)

    run_async(skills_sync.remove_all(UID))  # must not raise
    assert calls == list(skills_sync.WHITELIST)


def test_remove_all_without_ai_base_is_noop(monkeypatch):
    monkeypatch.setattr(skills_sync, "_read_ai_base", lambda: None)

    async def boom(base, uid, skill_id):
        raise AssertionError("must not be called")

    monkeypatch.setattr(skills_sync, "_post_remove", boom)
    run_async(skills_sync.remove_all(UID))  # no exception


# --------------------------------------------------------------------------
# X-Internal-Token: N2 -- Go's /_internal/skills/install|remove now requires
# it (route/v2.go wires v2.InternalTokenOnly), so the Python side must send
# it the same way channels/credentials.py does.
# --------------------------------------------------------------------------


class _FakeAsyncClient:
    """Minimal httpx.AsyncClient stand-in that records the last POST call's
    headers, mirroring the FakeAsyncClient pattern in test_mcp_runtime.py."""

    calls: list

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def post(self, url, json=None, headers=None):
        self.__class__.calls.append({"url": url, "json": json, "headers": headers or {}})

        class _Resp:
            status_code = 200

            def json(self):
                return {"id": json.get("skill", {}).get("name") if json else None}

        return _Resp()


def test_post_install_sends_internal_token(monkeypatch):
    monkeypatch.setattr(skills_sync, "_internal_token", lambda: "known-token-value")
    _FakeAsyncClient.calls = []
    monkeypatch.setattr(skills_sync.httpx, "AsyncClient", _FakeAsyncClient)

    run_async(skills_sync._post_install("http://127.0.0.1:9999", UID, {"name": "lark-base"}))

    assert len(_FakeAsyncClient.calls) == 1
    assert _FakeAsyncClient.calls[0]["headers"].get("X-Internal-Token") == "known-token-value"


def test_post_remove_sends_internal_token(monkeypatch):
    monkeypatch.setattr(skills_sync, "_internal_token", lambda: "known-token-value")
    _FakeAsyncClient.calls = []
    monkeypatch.setattr(skills_sync.httpx, "AsyncClient", _FakeAsyncClient)

    run_async(skills_sync._post_remove("http://127.0.0.1:9999", UID, "lark-base"))

    assert len(_FakeAsyncClient.calls) == 1
    assert _FakeAsyncClient.calls[0]["headers"].get("X-Internal-Token") == "known-token-value"


def test_post_install_logs_and_still_sends_when_token_unreadable(monkeypatch, caplog):
    monkeypatch.setattr(skills_sync, "_internal_token", lambda: None)
    _FakeAsyncClient.calls = []
    monkeypatch.setattr(skills_sync.httpx, "AsyncClient", _FakeAsyncClient)

    with caplog.at_level("WARNING"):
        run_async(skills_sync._post_install("http://127.0.0.1:9999", UID, {"name": "lark-base"}))

    assert len(_FakeAsyncClient.calls) == 1
    assert "X-Internal-Token" not in _FakeAsyncClient.calls[0]["headers"]
    assert any("internal token unreadable" in r.message for r in caplog.records)
