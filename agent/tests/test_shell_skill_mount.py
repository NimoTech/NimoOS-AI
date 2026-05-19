import os
from pathlib import Path

import pytest

from skills.shell import _build_argv, SANDBOX_SKILLS_VAR


def test_build_argv_mounts_skills_when_var_set(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    skills_view = tmp_path / "view"
    skills_view.mkdir()
    token = SANDBOX_SKILLS_VAR.set(str(skills_view))
    try:
        argv = _build_argv(work, "echo hi")
    finally:
        SANDBOX_SKILLS_VAR.reset(token)

    bound = False
    for i, tok in enumerate(argv):
        if tok == "--ro-bind" and argv[i+1] == str(skills_view) and argv[i+2] == "/skill":
            bound = True
            break
    assert bound, f"--ro-bind {skills_view} /skill not in argv: {argv}"


def test_build_argv_no_skills_mount_when_var_unset(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    # Default ContextVar value is empty string.
    argv = _build_argv(work, "echo hi")
    assert "/skill" not in argv


def test_build_argv_concurrent_contexts_dont_leak():
    """The whole point of using a ContextVar: two parallel coroutines
    each see their own value of SANDBOX_SKILLS_VAR."""
    import asyncio

    async def coroutine(view, work):
        token = SANDBOX_SKILLS_VAR.set(view)
        try:
            await asyncio.sleep(0.01)  # yield to the other coroutine
            argv = _build_argv(Path(work), "echo")
            return any(
                argv[i] == "--ro-bind" and argv[i+1] == view and argv[i+2] == "/skill"
                for i in range(len(argv) - 2)
            )
        finally:
            SANDBOX_SKILLS_VAR.reset(token)

    async def runner():
        return await asyncio.gather(
            coroutine("/view-a", "/tmp"),
            coroutine("/view-b", "/tmp"),
        )

    a, b = asyncio.run(runner())
    assert a and b, "Each coroutine should see its own mount path"
