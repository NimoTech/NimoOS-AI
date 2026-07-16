"""Append-only security audit log, independent of agent.db.

Records security-relevant events (dangerous shell commands + verdicts,
confirmation approvals, egress grants, file write/delete). Writes JSON lines
with O_APPEND only — never seeks or truncates — so a running agent cannot
rewrite history through this module. A root-compromised agent could still
tamper with the file directly, so install.sh / deploy-agent.sh set `chattr +a`
on this log at deploy time — the kernel then permits append-only opens even
from inside the container, blocking truncate/delete/rewrite. (Not absolute:
filesystems without +a support fall back to module-level O_APPEND only; log
rotation must `chattr -a` first.)

audit() NEVER raises: auditing is a side-channel and must not break the
operation being audited.
"""
from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger("nimoos-agent")

_DEFAULT_PATH = "/var/lib/nimoos/ai/agent/audit.log"
_TEST_PATH: str | None = None


def _audit_path() -> str:
    if _TEST_PATH is not None:
        return _TEST_PATH
    return os.environ.get("NIMOOS_AGENT_AUDIT_LOG", _DEFAULT_PATH)


def set_audit_path_for_test(p: str) -> None:
    global _TEST_PATH
    _TEST_PATH = p


def audit(event: str, *, user_id=None, session_id=None, **fields) -> None:
    try:
        rec = {
            "ts": int(time.time()),
            "event": str(event),
            "user_id": user_id,
            "session_id": session_id,
        }
        rec.update(fields)
        # default=str so a stray non-serializable field can't blow up auditing
        line = json.dumps(rec, ensure_ascii=False, default=str)
        path = _audit_path()
        # O_APPEND: atomic append, never truncate/seek. 0o600: not world-readable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception as exc:  # noqa: BLE001 — audit must never break the caller
        logger.warning("audit: failed to write event %r: %s", event, exc)
