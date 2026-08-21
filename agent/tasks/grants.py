"""grants.py — pre-authorization helpers run once at the start of a scheduled
task's unattended run (M2 scheduled agent tasks).

Two independent grants, both driven by a task's `preauth` document
(see `tasks/preauth.py`):

* :func:`grant_fs` writes the task's `fs_write` directories into
  `visible_resources` for the run's (freshly created) session. Because a
  scheduled task always runs in a brand-new session, session-scoped
  visibility *is* run-scoped visibility — there is no broader session to leak
  into. This mirrors the `kind=init` branch in `main.py`'s chat endpoint,
  down to the same `INSERT ... ON CONFLICT(session_id, path) DO NOTHING`.

* :func:`grant_egress` pre-registers a byte-budget ticket with the
  egress-proxy for each preauthorized domain via `egress.grant.register_grant`.

  **This only pre-pays an upload byte budget — it does NOT open the domain
  for outbound connections.** Domain allow-listing for an unattended run is a
  separate mechanism: Task 4's driver answers the egress-proxy's
  confirmation card using the preauth document. Skipping `grant_egress` does
  not block egress; it only means a large upload will trip the proxy's
  byte-budget confirm flow an extra time. Skipping Task 4's driver wiring
  *would* block egress entirely, since no connection could be confirmed at
  all. Both pieces are required; this module only provides the first.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from egress import grant as _egress_grant

logger = logging.getLogger("nimoos-agent")

# Matches the CHECK constraint on visible_resources.kind (db.py) — fs_write
# entries are always directories, never individual files.
_KIND_FOLDER = "folder"


def grant_fs(conn, session_id: str, paths: list[str]) -> int:
    """Write preauthorized `fs_write` directories into `visible_resources`.

    Skips (without raising) anything that is not an absolute path or does not
    resolve to an existing directory — those are exactly the paths that would
    also be useless to the fs skill layer, so there is no permission granted
    that would ever be honored. Idempotent: re-granting the same path for the
    same session inserts nothing further (ON CONFLICT DO NOTHING) but is
    still counted, since the grant *is* in effect either way.

    Returns the number of paths actually granted (i.e. valid absolute,
    existing directories) — not the number of rows newly inserted.
    """
    if not paths:
        return 0
    now = int(time.time())
    granted = 0
    for path in paths:
        if not isinstance(path, str) or not path:
            continue
        if not os.path.isabs(path):
            continue
        if not os.path.isdir(path):
            continue
        conn.execute(
            "INSERT INTO visible_resources (session_id, path, kind, added_at) "
            "VALUES (?,?,?,?) ON CONFLICT(session_id, path) DO NOTHING",
            (session_id, path, _KIND_FOLDER, now),
        )
        granted += 1
    conn.commit()
    return granted


async def grant_egress(
    domains: list[str],
    *,
    max_bytes: int = 10 * 1024 * 1024,
    ttl_sec: int = 3600,
) -> dict[str, bool]:
    """Pre-register a byte-budget grant with the egress-proxy for each domain.

    `register_grant` is synchronous urllib (up to a few seconds per call), so
    each call runs in the default executor to keep the event loop free —
    same pattern as `skills/shell.py`'s A-path upload grant.

    Grants are keyed by a **bare host, no port** — the egress-proxy's
    consumer side strips the port before ever looking a grant up:
    `handleConnect` does `net.SplitHostPort(hostport)` and `pumpUploadGated`
    calls `hasGrant(host)` / `consumeGrant(host, ...)` with that bare host
    (`deploy/agent/egress-proxy/main.go`). A key with `:443` attached would
    simply never match, silently making every grant here dead weight. The
    existing A-path precedent (`skills/shell.py`) agrees: it passes
    `intent.host`, which comes from `urlparse().hostname` in
    `egress/parse.py` and never carries a port. If a preauthorized domain
    happens to be written with a port (e.g. `"api.example.com:443"` in a
    task's `fs_write`/`egress_domains` document), the port is stripped before
    registering — never appended.

    Never raises: a failure to reach the egress-proxy (or any other
    unexpected error) is recorded as `False` for that domain and the loop
    continues — the safe fallback is the proxy's normal confirm/block flow,
    not aborting the run.

    Returns `{domain: granted}` keyed by the *original* domain strings (not
    the bare host used for registration), so the caller can match results
    back against the preauth document's `egress_domains` list.
    """
    results: dict[str, bool] = {}
    if not domains:
        return results
    loop = asyncio.get_running_loop()
    for domain in domains:
        if not isinstance(domain, str) or not domain:
            continue
        # Bare host only — see the docstring above for why appending a port
        # would make the grant unmatchable by the proxy.
        host = domain.rsplit(":", 1)[0] if ":" in domain else domain
        try:
            ok = await loop.run_in_executor(
                None,
                lambda h=host: _egress_grant.register_grant(
                    h, max_bytes=max_bytes, ttl_sec=ttl_sec
                ),
            )
        except Exception as exc:  # noqa: BLE001 — never let a grant abort the run
            logger.warning(
                "grants: register_grant raised unexpectedly for host=%s: %s",
                host, exc,
            )
            ok = False
        results[domain] = bool(ok)
    return results
