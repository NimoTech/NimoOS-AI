"""TaskRunDriver — consume a RunSink for an UNATTENDED (scheduled) run.

`channels/driver.py` drives a run for a human sitting in Telegram/Discord: it
streams text out and hands confirmation cards to a router that renders buttons.
A scheduled task has nobody to press them, so this driver does the opposite —
it answers every confirmation itself, immediately, from the task's `preauth`
document, and returns a result dict the runner writes into `task_runs`.

**Why answering matters (this is the hinge of M2, measured 2026-08-16).**
The egress-proxy's TOFU cache and `egress.grant.register_grant` are two
independent mechanisms: a grant only pre-pays an upload BYTE budget; opening a
domain for outbound traffic happens exclusively through a confirmation card —
proxy `callConfirm` → agent `/internal/egress-confirm` → an event pushed to the
currently active session → a human answer → proxy `markConfirmed(host)`, cached
in process memory for an hour.  A scheduled run *is* the active session, so the
card lands here.  If this driver blanket-denied, every task needing the network
(Feishu included) would fail forever; if it simply ignored cards, the tool
coroutine would sit on `ConfirmManager.wait()` for its 24h default.  So: decide
from `preauth`, and **never await** a decision.

Deliberate asymmetry with the run-level injections done by Task 2/3: shell and
MCP pre-authorization is applied *before* the tool runs (run-level allowlist /
`pre_confirmed_tools`), so a card for those kinds means the command was NOT
pre-authorized.  Reaching this driver is therefore already the denial path for
them — we only record it (Task 7 turns those records into new preauth rules).
"""
from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger("nimoos-agent.tasks")

_CONFIRM_TYPES = ("access_request", "confirmation_required")

# Canonical `denied` / `auto_approved` kinds.  These are the vocabulary Task 7's
# from-denied generator maps back into preauth buckets, so they are normalized
# here rather than passed through raw (a raw `access_request` card's own `kind`
# field means "file" vs "folder" — a different taxonomy entirely).
_KIND_EGRESS = "egress"
_KIND_FS = "fs"
_KIND_SHELL = "shell"

# First non-empty of these becomes the `detail` of a recorded action.
_DETAIL_FIELDS = ("host", "path", "command", "title", "name", "url", "description")


def _strip_port(host: str) -> str:
    """`example.com:443` -> `example.com`; IPv6-safe.

    Only a single colon is treated as a port separator, so a bare IPv6 literal
    (`::1`) is left alone; a bracketed one (`[::1]:443`) is unwrapped.
    """
    h = (host or "").strip()
    if h.startswith("["):
        end = h.find("]")
        return h[1:end] if end != -1 else h
    if h.count(":") == 1:
        return h.split(":", 1)[0]
    return h


def _norm_host(host: str) -> str:
    return _strip_port(host).lower().rstrip(".")


def egress_allowed(host: str, domains) -> bool:
    """True if `host` is covered by one of the preauthorized `domains`.

    `domains` must be the list `preauth.parse()` produces.  A bare string must
    NOT be iterated (it would decompose into chars, and a one-char "domain"
    matches nothing useful but proves the shape was never validated), and
    neither must a dict (iterating one yields its KEYS, which would silently
    authorize them).  Both are rejected outright rather than trusted to the
    caller: this function is the gate that decides whether an unattended run
    reaches the network, so it validates its own input.

    Case-insensitive, port-insensitive, trailing-dot-insensitive.  `*.a.com`
    matches any subdomain of `a.com` but NOT the apex `a.com` (the author has
    to name it explicitly) and never a suffix-confusion neighbour such as
    `evil-a.com`.  A bare `*` is NOT a wildcard for everything — it would have
    to be written per domain, and letting one character open all egress in an
    unattended run is not a trade this file makes.
    """
    if isinstance(domains, str) or not isinstance(domains, (list, tuple)):
        return False
    h = _norm_host(host)
    if not h or not domains:
        return False
    for raw in domains:
        if not isinstance(raw, str):
            continue
        d = _norm_host(raw)
        if not d:
            continue
        if d.startswith("*."):
            suffix = d[1:]                       # ".a.com"
            if h.endswith(suffix) and len(h) > len(suffix):
                return True
        elif h == d:
            return True
    return False


def _real(path: str) -> str | None:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return None


def fs_allowed(path: str, roots) -> bool:
    """True if `path` resolves inside one of the preauthorized `roots`.

    realpath on BOTH sides (so a symlink cannot smuggle a write out of an
    authorized tree), then a component-boundary comparison — `/DATA/reports`
    must never authorize `/DATA/reports-evil`.  Stricter than
    `grants.grant_fs`, which only does `os.path.isdir` on the roots; the two
    are independent gates and this one is the one that can be attacked with a
    crafted path, so it resolves.

    `roots` must be the list `preauth.parse()` produces.  A bare string must
    NOT be iterated: `"/DATA/reports"` would decompose into chars whose first
    element is `"/"` — i.e. a malformed document would grant the WHOLE
    filesystem.  A dict would iterate its keys.  Both are rejected here rather
    than trusted to the caller, for the same reason as `egress_allowed`.
    """
    if isinstance(roots, str) or not isinstance(roots, (list, tuple)):
        return False
    if not path or not isinstance(path, str) or not roots:
        return False
    p = _real(path)
    if p is None:
        return False
    for raw in roots:
        if not isinstance(raw, str) or not raw:
            continue
        r = _real(raw)
        if r is None:
            continue
        if p == r or p.startswith(r.rstrip(os.sep) + os.sep):
            return True
    return False


def _kind_of(ev: dict) -> str:
    """Normalized bucket name for a confirmation event."""
    if ev.get("type") == "access_request":
        return _KIND_FS
    action = ev.get("action")
    if action == "egress_confirm":
        return _KIND_EGRESS
    if isinstance(action, str) and action.startswith("shell"):
        return _KIND_SHELL
    return action or ev.get("kind") or ev.get("type") or "unknown"


def _detail_of(ev: dict) -> str:
    server, tool = ev.get("server"), ev.get("tool")
    if isinstance(server, str) and server and isinstance(tool, str) and tool:
        return f"{server}::{tool}"               # the shape Task 7 wants for mcp_tool
    for key in _DETAIL_FIELDS:
        v = ev.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


class TaskRunDriver:
    def __init__(self, *, confirm_mgr, session_id: str, preauth: dict,
                 run_timeout: float, sleep=asyncio.sleep, now=None):
        self._mgr = confirm_mgr
        self._session_id = session_id
        self._preauth = preauth or {}
        self._run_timeout = run_timeout
        # `sleep` is accepted for ctor symmetry with ChannelRunDriver (and so a
        # future pacing need has a seam); nothing on this path sleeps — an
        # unattended run has no rate-limited chat to pace against, and its only
        # time bound is the absolute deadline below.
        self._sleep = sleep
        # Injectable clock. Defaults to the loop clock, resolved inside drive()
        # because there is no running loop at construction time.
        self._now = now
        self._final = ""          # authoritative text from a terminal `message`
        self._delta = ""          # accumulated `message_delta` chunks
        self._error = ""
        self._hard_error = False  # an `error` event, as opposed to a soft note
        self._denied: list[dict] = []
        self._auto_approved: list[dict] = []

    # -- confirmations ----------------------------------------------------

    def _decide(self, ev: dict) -> tuple[bool, str]:
        """(approve, offending_detail).

        `offending_detail` is non-empty only when a specific element of the
        request is what sank it — currently the first non-preauthorized path of
        a batch fs card.  Recording `paths[0]` there would name a path that was
        actually ALLOWED and hide the one that was not, and Task 7's from-denied
        generator would then propose a rule that changes nothing.
        """
        kind = _kind_of(ev)
        if kind == _KIND_EGRESS:
            return egress_allowed(ev.get("host") or "",
                                  self._preauth.get("egress_domains") or []), ""
        if kind == _KIND_FS:
            roots = self._preauth.get("fs_write") or []
            # A batch card (`request_access_batch`) carries every path in
            # `paths`; the user would approve or deny the whole set atomically,
            # so we approve only if EVERY path is preauthorized.
            paths = ev.get("paths")
            if not isinstance(paths, (list, tuple)) or not paths:
                paths = [ev.get("path") or ""]
            for p in paths:
                if not fs_allowed(p, roots):
                    return False, (p if isinstance(p, str) else "")
            return True, ""
        # shell / mcp_tool / mcp_install / toolbox_install / elicitation …
        # Their pre-authorization is injected before the tool runs, so a card
        # here means "not preauthorized".
        return False, ""

    def _handle_confirm(self, ev: dict) -> None:
        # Classification and matching run INSIDE the try: a malformed event
        # (e.g. a non-string `host`) must not escape into drive(), because the
        # confirmations registered AFTER it would then have nobody to answer
        # them and would park their tool coroutines on wait()'s 24h default.
        kind, approve, detail = "unknown", False, ""
        try:
            kind = _kind_of(ev)
            approve, offending = self._decide(ev)
            detail = offending or _detail_of(ev)
        except Exception:                   # noqa: BLE001 — deny, never propagate
            logger.warning("task driver: malformed confirmation event (kind=%s); "
                           "denying", kind, exc_info=True)
            approve, detail = False, ""
        record = {"kind": kind, "detail": detail}
        cid = ev.get("confirm_id")
        if not cid:
            # Nothing is waiting on an id-less card (elicitation cards get their
            # id added by the caller); record it so the run still reports what
            # it could not do.
            logger.warning("task driver: confirmation without confirm_id: %s",
                           record["kind"])
            self._denied.append(record)
            return
        try:
            self._mgr.resolve(cid, approve, expected_session_id=self._session_id)
        except Exception as exc:            # noqa: BLE001 — KeyError for expired /
            # session-mismatched confirms, and anything else: a raise here would
            # abandon the rest of the stream, so it is swallowed and recorded.
            logger.warning("task driver: resolve(%s, %s) failed: %s",
                           cid, approve, exc)
            self._denied.append(record)
            return
        (self._auto_approved if approve else self._denied).append(record)

    # -- event stream -----------------------------------------------------

    def _apply(self, ev: dict) -> bool:
        """Fold one event in. Returns True on the terminal `done`."""
        t = ev.get("type")
        if t == "message_delta":
            self._delta += ev.get("content") or ""
        elif t == "message":
            # Streaming models emit deltas and SUPPRESS this event; the
            # non-streaming path emits only this one. See channels/collector.py.
            self._final = ev.get("content") or ""
        elif t in _CONFIRM_TYPES:
            self._handle_confirm(ev)
        elif t == "error":
            self._error = self._error or (ev.get("content") or "agent error")
            self._hard_error = True
        elif t == "max_turns_exceeded":
            note = f"max_turns_exceeded: hit the {ev.get('max_turns') or 0}-turn limit"
            self._error = f"{note}; {self._error}" if self._error else note
        return t == "done"

    def _result(self, status: str) -> dict:
        summary = self._final or self._delta
        if status != "timeout":
            if self._hard_error or (self._error and not summary):
                status = "failed"
        return {"status": status, "summary": summary, "error": self._error,
                "denied": self._denied, "auto_approved": self._auto_approved}

    async def drive(self, sink) -> dict:
        q = None
        try:
            past, q = sink.subscribe()
            for ev in past:
                if self._apply(ev):
                    return self._result("succeeded")
            loop = asyncio.get_running_loop()
            now = self._now or loop.time
            deadline = now() + self._run_timeout
            while True:
                remaining = deadline - now()
                if remaining <= 0:
                    self._error = self._error or "timeout"
                    return self._result("timeout")
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    self._error = self._error or "timeout"
                    return self._result("timeout")
                if self._apply(ev):
                    return self._result("succeeded")
        finally:
            if q is not None:
                try:
                    sink.unsubscribe(q)
                except Exception:            # noqa: BLE001
                    logger.warning("task driver: unsubscribe failed", exc_info=True)
            # Backstop: anything still pending for this session (a card emitted
            # after we stopped reading, or one we failed to resolve) is released
            # as denied, so no tool coroutine is left on a 24h wait().
            try:
                self._mgr.cancel_session(self._session_id)
            except Exception:                # noqa: BLE001
                logger.warning("task driver: cancel_session failed", exc_info=True)
