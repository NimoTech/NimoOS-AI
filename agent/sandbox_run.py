"""One-shot sandbox agent run for skill testing.

POST /agent/sandbox-run starts a temporary, no-DB chat with a skill's
SKILL.md baked into the system prompt. bwrap runs with --unshare-net and
a fresh tmpfs for /work; the bundle is ro-mounted at /skill.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path

from run_sink import RunSink


SANDBOX_TIMEOUT_SEC = 90


def build_sandbox_prompt(bundle_dir: str, user_prompt: str) -> str:
    """Inject the skill's SKILL.md and the user prompt into a system prompt."""
    md_path = Path(bundle_dir) / "manifest.json"
    md = (Path(bundle_dir) / "SKILL.md").read_text()
    try:
        manifest = json.loads(md_path.read_text())
    except (OSError, json.JSONDecodeError):
        manifest = {"id": "unknown"}
    return (
        f"You are running skill `{manifest.get('id')}` in a sandbox.\n"
        f"Its SKILL.md follows; follow it.\n\n"
        f"--- SKILL.md ---\n{md}\n--- end SKILL.md ---\n\n"
        f"User task: {user_prompt}\n"
    )


async def run_sandbox(
    *,
    runner,
    bundle_dir: str,
    user_prompt: str,
    user_id: str,
    skills_root: str,
    provider_key: str,
    provider_url: str,
    model: str,
    provider_type: str,
    sink: RunSink,
):
    """Spin up a one-shot agent run that does NOT persist to the DB.

    Concurrency contract (Fix 1.1):
      * Sandbox view path is conveyed via skills.shell.SANDBOX_SKILLS_VAR
        (ContextVar), NOT os.environ.
      * Shell root is conveyed via SANDBOX_SHELL_ROOT_VAR.
      * Two concurrent sandbox-runs each see their own values — no leakage.
    """
    sandbox_dir = tempfile.mkdtemp(prefix="nimoos-sandbox-")
    bundle_view = os.path.join(sandbox_dir, "view")
    os.makedirs(bundle_view)
    bundle_name = os.path.basename(bundle_dir)
    os.symlink(bundle_dir, os.path.join(bundle_view, bundle_name))

    work = os.path.join(sandbox_dir, "work")
    os.makedirs(work)

    # Per-coroutine context: these tokens reset on `finally`.
    import skills.shell as shell_skills
    sk_token = shell_skills.SANDBOX_SKILLS_VAR.set(bundle_view)
    sr_token = shell_skills.SANDBOX_SHELL_ROOT_VAR.set(sandbox_dir)

    session_id = "sandbox-" + uuid.uuid4().hex[:8]
    prompt = build_sandbox_prompt(bundle_dir, user_prompt)
    try:
        await asyncio.wait_for(
            runner.run(
                session_id, user_id, prompt, sink,
                provider_key, provider_url, model,
                kind="chat",
                provider_type=provider_type,
                run_id=session_id,
                attachment_ids=(),
            ),
            timeout=SANDBOX_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        await sink.put({"type": "error", "content": "sandbox timed out"})
    finally:
        shell_skills.SANDBOX_SKILLS_VAR.reset(sk_token)
        shell_skills.SANDBOX_SHELL_ROOT_VAR.reset(sr_token)
        await sink.put({"type": "done"})
        try:
            import shutil
            shutil.rmtree(sandbox_dir, ignore_errors=True)
        except Exception:
            pass
