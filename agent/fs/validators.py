"""Pure (no-prompt, no-write) path classification for batch preflight.

Mirrors the resolve+gate logic in ops.py but never triggers an interactive
access-request and never mutates disk. Returns a category so the batch engine
can aggregate need_grant / blocked / errors across many ops.
"""
from __future__ import annotations

import os
from typing import Tuple

from fs import paths, ignore


def _visible_roots(ctx) -> list[str]:
    return [r["path"] for r in ctx["conn"].execute(
        "SELECT path FROM visible_resources WHERE session_id=? AND kind='folder'",
        (ctx["session_id"],))]


def classify(ctx, raw: str) -> Tuple[str, str]:
    """Return (category, abs_path). category in {ok, need_grant, blocked}.

    - ok:        resolvable AND inside visible scope AND not ignored.
    - blocked:   hits hard blacklist / implicit-ignore / gitignore.
    - need_grant: out of scope but anchorable (could be granted).
    """
    try:
        abs_ = paths.resolve(raw, ctx["session_id"], ctx["conn"])
    except paths.PermissionDenied:
        # Out of scope. But a blacklisted out-of-scope path must surface as
        # blocked, not grantable — re-run gate on the anchored candidate.
        try:
            anchored = os.path.realpath(
                paths.anchor(raw, ctx["session_id"], ctx["conn"]))
        except paths.PermissionDenied:
            return ("blocked", raw)  # un-anchorable → treat as non-grantable
        probe = anchored if not os.path.isdir(anchored) else \
            os.path.join(anchored, "__nimoos_access_probe__")
        try:
            ignore.gate(probe, _visible_roots(ctx), ctx.get("user_patterns", []))
        except (ignore.BlockedHardBlacklist, ignore.BlockedImplicit,
                ignore.BlockedGitignore):
            return ("blocked", anchored)
        return ("need_grant", anchored)
    # Resolved inside scope; still gate for blacklist/ignore.
    try:
        ignore.gate(abs_, _visible_roots(ctx), ctx.get("user_patterns", []))
    except (ignore.BlockedHardBlacklist, ignore.BlockedImplicit,
            ignore.BlockedGitignore):
        return ("blocked", abs_)
    return ("ok", abs_)
