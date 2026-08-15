"""Headless Feishu (lark-cli) device-flow binding state machine.

RECORDED, NOT GUESSED. Every stream/field choice below was probed against the
real lark-cli v1.0.85 on 118 with a throwaway HOME; the raw captures live in
``agent/tests/fixtures/lark/``. The important deviations from the plan:

* **All output goes to stderr.** ``config show``, ``auth status``, ``whoami``,
  ``auth login`` and ``config init`` all leave stdout empty and write their
  JSON envelope (or the QR block + URL) to stderr. So both the URL scrape and
  the JSON parse read stderr, with stdout kept only as a fallback.
* ``config init --new`` has **no ``--json``**. It prints an ASCII QR block,
  then ``打开以下链接配置应用:`` and a bare URL line
  (``https://open.feishu.cn/page/cli?user_code=...&lpv=...&from=cli``), then
  blocks on ``等待配置应用...`` until the user finishes in the browser.
* "not configured" is exit code **3** with
  ``{"ok":false,"error":{"type":"config","subtype":"not_configured",...}}``.
  Exit code 0 is *not* a reliable success signal on its own for ``auth
  logout`` (it prints "No configuration found." and still exits 0).
* ``auth login --no-wait --json`` yields ``verification_url`` + ``device_code``
  (confirmed from the CLI binary's own struct tags — ``json:"device_code"``,
  ``json:"verification_uri"``, ``json:"verification_uri_complete"`` — and from
  the bundled ``lark-shared`` skill doc). The *enclosing envelope* could not be
  recorded, because that step needs a completed app config, which needs a real
  human clicking through Feishu. So the parser searches the decoded JSON
  **recursively** for those keys rather than pinning a path, and accepts the
  documented aliases. Likewise the ``bound`` shape of ``whoami``.
  -> Both are flagged for calibration at Task 11 real-machine acceptance.
* There is no ``lark-cli auth whoami``; the top-level ``whoami`` command is
  real (hidden from ``--help``'s command list but ``help whoami`` documents it)
  and is what we use to refresh true bound/unbound state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Iterable

_LOG = logging.getLogger("nimoos-agent.lark")

# Default location inside the agent container (toolbox volume). The env var is
# the seam tests inject a fake CLI through; `lark_bin()` re-reads it every call
# so monkeypatch.setenv works after import.
LARK_BIN = os.environ.get("NIMOOS_LARK_CLI", "/opt/toolbox/bin/lark-cli")

MAX_LOG_BYTES = 8 * 1024

# Business domains requested at binding time (matches the plan).
LOGIN_DOMAINS = "base,docs,im,drive"

# Timeouts (seconds). The two interactive steps intentionally get a long
# ceiling: they block on a human in a browser, and restarting them invalidates
# the previous device code (per the CLI's own guidance).
PROBE_TIMEOUT = 5
LOGOUT_TIMEOUT = 10
LOGIN_INIT_TIMEOUT = 60
INTERACTIVE_TIMEOUT = 15 * 60

PHASES = ("unbound", "starting", "await_verify", "polling", "bound", "failed")

# Synthetic exit codes. Kept out of the CLI's own range (it uses 2/3/4/5) so a
# probe failure is never mistaken for a real answer — notably "not configured",
# which would otherwise send us off to create a brand-new Feishu app.
RC_TIMEOUT = 124
RC_NO_CLI = 127

_URL_RE = re.compile(r"https://\S+")

# Anything whose *key* matches gets its value replaced before it can reach the
# log tail or the identity projection. Applied recursively.
_SECRET_KEY_RE = re.compile(r"token|secret|credential|cookie|password|passwd|api[_-]?key", re.I)
# Keys that match the regex but carry no secret — `token_status` is literally
# the logged-in/logged-out flag the UI wants to show.
_SAFE_KEYS = frozenset({"token_status", "tokenstatus", "token_expires_at"})
_REDACTED = "[redacted]"

# Plain-text fallback for streams that are not JSON: `access_token: xxx`,
# `"refresh_token" = xxx`. Group 1 is the key, group 2 the separator.
_SECRET_LINE_RE = re.compile(
    r'("?[\w.-]*(?:token|secret|credential|cookie|password|passwd|api[_-]?key)[\w.-]*"?)'
    r'(\s*[:=]\s*)\S+',
    re.I,
)

# Identity is echoed to the UI, so it is a whitelist projection: unknown keys
# are dropped rather than passed through. Anything not listed here simply does
# not reach the browser.
_IDENTITY_KEYS = frozenset({
    "name", "user_name", "en_name", "nick_name", "display_name",
    "user_id", "open_id", "union_id", "employee_id",
    "tenant_key", "tenant_name", "app_id", "app_name",
    "identity", "identity_type", "profile", "brand", "domain",
    "status", "token_status", "expires_at", "avatar_url", "email",
})

# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


class _State:
    __slots__ = ("phase", "verify_url", "identity", "error", "log", "task", "sync_task")

    def __init__(self) -> None:
        self.phase: str = "unbound"
        self.verify_url: str = ""
        self.identity: dict | None = None
        self.error: str = ""
        self.log: str = ""
        self.task: asyncio.Task | None = None
        # Fire-and-forget lark skills registration kicked off once `_flow`
        # reaches `bound` (see `_sync_skills_after_bind`). Never awaited by
        # production code -- binding success must not depend on it -- but
        # tests hold onto it to make the otherwise-async hook deterministic.
        self.sync_task: asyncio.Task | None = None

    def snapshot(self) -> dict:
        return {
            "phase": self.phase,
            "verify_url": self.verify_url,
            "identity": self.identity,
            "error": self.error,
            "log": self.log,
        }


_STATES: dict[str, _State] = {}
_LOCK = asyncio.Lock()


def _state(uid: str) -> _State:
    st = _STATES.get(uid)
    if st is None:
        st = _STATES[uid] = _State()
    return st


def reset_all() -> None:
    """Drop all in-memory state (test hook).

    Also rebinds `_LOCK`. An asyncio.Lock only binds itself to a loop on its
    first *contended* acquire, and then raises if awaited from a different one
    — which is exactly what happens across successive `asyncio.run()` calls in
    the test suite. Production has a single loop and never hits this.
    """
    global _LOCK
    for st in _STATES.values():
        if st.task and not st.task.done():
            st.task.cancel()
        if st.sync_task and not st.sync_task.done():
            st.sync_task.cancel()
    _STATES.clear()
    _LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def lark_bin() -> str:
    return os.environ.get("NIMOOS_LARK_CLI") or LARK_BIN


def user_home(uid: str) -> Path:
    """Per-user HOME for the CLI, reusing shell.py's HOMES_ROOT.

    Imported lazily and read off the module (not `from ... import HOMES_ROOT`)
    so tests can monkeypatch `skills.shell.HOMES_ROOT`.
    """
    from skills import shell

    home = Path(shell.HOMES_ROOT) / uid
    home.mkdir(parents=True, exist_ok=True)
    return home


_UID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def valid_uid(uid: str) -> bool:
    """Reject anything that could escape HOMES_ROOT (`../`, `/`, empty, NUL).

    Same shape of hole as Task 7's: `uid` lands in a filesystem path, so it is
    validated as an opaque token rather than sanitised.
    """
    return bool(uid) and _UID_RE.fullmatch(uid) is not None


def _redact(value, _depth: int = 0):
    """Recursively blank out secret-looking values by key name.

    lark-cli's success envelopes are not fully known (see module docstring), so
    anything we surface to the UI — the log tail and the identity blob — goes
    through here first: an envelope that happens to carry `access_token` must
    not reach a browser just because we did not anticipate the field.
    """
    if _depth > 12:
        return value
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = str(k)
            if key.lower() not in _SAFE_KEYS and _SECRET_KEY_RE.search(key):
                out[key] = _REDACTED
            else:
                out[key] = _redact(v, _depth + 1)
        return out
    if isinstance(value, list):
        return [_redact(v, _depth + 1) for v in value]
    return value


def _redact_text(text: str) -> str:
    """Redact a raw CLI stream before it becomes the UI-visible log tail.

    Re-serialises the JSON envelope when there is one; otherwise falls back to
    a line-oriented `key: value` / `key=value` scrub so a plain-text leak (or a
    half-written stream we could not decode) is still covered.
    """
    doc = _parse_json(text)
    if doc is not None:
        try:
            return json.dumps(_redact(doc), ensure_ascii=False, indent=2)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            pass
    return _SECRET_LINE_RE.sub(r"\1\2" + _REDACTED, text or "")


def _clip(text: str) -> str:
    """Truncate to MAX_LOG_BYTES, keeping the tail (where errors land)."""
    raw = (text or "").encode("utf-8", "replace")
    if len(raw) <= MAX_LOG_BYTES:
        return raw.decode("utf-8", "replace")
    return "...[truncated]\n" + raw[-(MAX_LOG_BYTES - 20):].decode("utf-8", "replace")


def _env(uid: str) -> dict:
    """Minimal environment: HOME + PATH (toolbox bin first) + LANG only.

    Deliberately built from scratch instead of copying os.environ — the agent
    process carries provider API keys, DB paths and netns config that must
    never reach a third-party CLI's process environment.
    """
    toolbox_bin = os.path.dirname(lark_bin()) or "/opt/toolbox/bin"
    return {
        "HOME": str(user_home(uid)),
        "PATH": f"{toolbox_bin}:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "en_US.UTF-8",
    }


def _parse_json(raw: str) -> Any:
    """Decode the first JSON value in `raw`, tolerating non-JSON preamble.

    lark-cli may prefix warnings (e.g. `[lark-cli] [WARN] proxy detected: ...`)
    before the JSON envelope, so a plain json.loads of the whole stream is not
    enough.
    """
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        pass
    decoder = json.JSONDecoder()
    for i, ch in enumerate(raw):
        if ch in "{[":
            try:
                return decoder.raw_decode(raw, i)[0]
            except ValueError:
                continue
    return None


def _find_key(doc: Any, keys: Iterable[str]) -> Any:
    """Breadth-first recursive lookup of the first alias present in `doc`.

    Used instead of a fixed path because the success envelope of
    `auth login --no-wait --json` could not be recorded (needs a real Feishu
    authorization). Alias order is honoured: the whole document is searched for
    keys[0] before falling back to keys[1], so a nested `verification_url`
    still wins over a top-level `verification_uri`.
    """
    for key in keys:
        queue = [doc]
        while queue:
            node = queue.pop(0)
            if isinstance(node, dict):
                if key in node and node[key] not in (None, ""):
                    return node[key]
                queue.extend(node.values())
            elif isinstance(node, list):
                queue.extend(node)
    return None


def _scrape_url(text: str) -> str | None:
    """First https:// URL in the stream, as an opaque string.

    Trailing punctuation is stripped conservatively; query params (`&lpv=`,
    `&from=cli`) must survive intact — the CLI docs are explicit that the URL
    must not be rewritten in any way.
    """
    m = _URL_RE.search(text or "")
    if not m:
        return None
    return m.group(0).rstrip(".,;)。，、")


def _err_summary(rc: int, stderr: str, step: str) -> str:
    """One-line, UI-visible failure summary. Scrubbed like the log tail is."""
    doc = _parse_json(stderr)
    if isinstance(doc, dict):
        err = doc.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("subtype") or err.get("type")
            if msg:
                return _SECRET_LINE_RE.sub(r"\1\2" + _REDACTED, f"{step}: {msg}")
    tail = (stderr or "").strip().splitlines()
    if tail:
        return _SECRET_LINE_RE.sub(r"\1\2" + _REDACTED, f"{step}: {tail[-1][:400]}")
    return f"{step}: exit {rc}"


async def _spawn(uid: str, args: list[str]):
    """create_subprocess_exec with the minimal env, or None if the CLI is absent."""
    try:
        return await asyncio.create_subprocess_exec(
            lark_bin(),
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_env(uid),
            cwd=str(user_home(uid)),
        )
    except OSError:
        # The toolbox component simply isn't installed yet — which is the
        # *default* state of a fresh box, and the first thing the settings page
        # asks about. Must degrade to "unbound", never a 500.
        return None


async def _run(uid: str, args: list[str], timeout: float) -> tuple[int, str, str]:
    """Run the CLI, capturing both streams. Returns (rc, stdout, stderr)."""
    proc = await _spawn(uid, args)
    if proc is None:
        return RC_NO_CLI, "", f"lark-cli not found at {lark_bin()}"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return RC_TIMEOUT, "", f"timed out after {timeout}s"
    except asyncio.CancelledError:
        # unbind() cancels the flow task mid-step. Without this the child
        # survives the cancellation: a still-polling `auth login --device-code`
        # would go on to write a fresh token into ~/.lark-cli *after* unbind's
        # rmtree, silently resurrecting the binding we were told to destroy.
        proc.kill()
        await proc.wait()
        raise
    return (
        proc.returncode or 0,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


async def _run_streaming_url(uid: str, args: list[str], st: _State, timeout: float):
    """Run a blocking step, scraping the verification URL out of stderr live.

    `config init --new` has no --json and blocks until the user finishes in the
    browser, so the URL must be pulled from the stream *while the process is
    still running* — waiting for exit would never surface it.
    """
    proc = await _spawn(uid, args)
    if proc is None:
        return RC_NO_CLI, f"lark-cli not found at {lark_bin()}"
    buf: list[str] = []

    async def _pump(stream, scrape: bool):
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace")
            buf.append(text)
            st.log = _clip(_redact_text("".join(buf)))
            if scrape and not st.verify_url:
                url = _scrape_url(text)
                if url:
                    st.verify_url = url
                    st.phase = "await_verify"

    try:
        await asyncio.wait_for(
            asyncio.gather(
                _pump(proc.stderr, True),
                _pump(proc.stdout, True),
            ),
            timeout=timeout,
        )
        rc = await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return RC_TIMEOUT, "".join(buf) + f"\ntimed out after {timeout}s"
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()  # reap, so unbind's rmtree cannot race a live child
        raise
    return rc, "".join(buf)


# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------


async def _is_configured(uid: str) -> int:
    """0 = configured, 1 = not configured, RC_TIMEOUT/RC_NO_CLI = unusable.

    Deliberately tri-state. Collapsing a probe failure into "not configured"
    would send the flow into `config init --new`, which really does create a
    brand-new Feishu app — an irreversible side effect triggered by a 5s
    timeout blip.
    """
    rc, _out, _err = await _run(uid, ["config", "show"], PROBE_TIMEOUT)
    if rc in (RC_TIMEOUT, RC_NO_CLI):
        return rc
    return 0 if rc == 0 else 1


async def _whoami(uid: str) -> dict | None:
    """Return the identity dict when genuinely logged in, else None.

    NOTE (Task 11): the exact bound-state envelope is unrecorded. We treat
    "exit 0 AND parsed JSON that is not an explicit ok:false / logged-out
    marker" as bound, which is the conservative reading of the recorded error
    shapes (every failure mode observed exits non-zero with ok:false).
    """
    rc, out, err = await _run(uid, ["whoami"], PROBE_TIMEOUT)
    if rc != 0:
        return None
    doc = _parse_json(out) or _parse_json(err)
    if not isinstance(doc, dict):
        return None
    if doc.get("ok") is False:
        return None
    status = _find_key(doc, ("token_status", "tokenStatus"))
    if isinstance(status, str) and status.lower() in ("loggedout", "logged_out", "none", ""):
        return None
    # Decide on the raw doc above, project for display below: the identity blob
    # is echoed straight to the browser.
    return _project_identity(doc)


def _project_identity(doc: dict) -> dict:
    """Whitelist-project a whoami envelope down to displayable identity fields.

    Unknown keys are dropped, not passed through — the bound-state envelope is
    unrecorded (Task 11), so an allow-list is the only way to be sure a token
    field we never anticipated does not reach the UI. Nested dicts are walked
    so `{"user": {"name": ...}}` survives.
    """
    out: dict = {}
    for k, v in doc.items():
        key = str(k)
        if isinstance(v, dict):
            nested = _project_identity(v)
            if nested:
                out[key] = nested
        elif key in _IDENTITY_KEYS:
            out[key] = _redact({key: v})[key]
    return out


# ---------------------------------------------------------------------------
# lark skills sync hook (Task 9)
# ---------------------------------------------------------------------------


async def _sync_skills_after_bind(uid: str) -> dict | None:
    """Fire-and-forget: register the embedded lark skills once binding
    succeeds.

    Scheduled via `asyncio.create_task` right after `_flow` sets
    `phase = "bound"` -- never awaited there, so a slow or failing sync can
    never turn a successful bind into a `failed` one. Any exception (import,
    subprocess, HTTP) is caught and only logged; `lark.skills_sync` is
    imported lazily to avoid a module-load-time circular import (it imports
    `binding` itself for subprocess plumbing).
    """
    try:
        from lark import skills_sync

        result = await skills_sync.sync(uid)
        if result.get("failed"):
            _LOG.warning("lark skills sync had failures for %s: %s", uid, result["failed"])
        return result
    except Exception:  # pragma: no cover - defensive, must never surface
        _LOG.warning("lark skills sync failed after bind for %s", uid, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# state machine
# ---------------------------------------------------------------------------


async def _flow(uid: str, st: _State) -> None:
    try:
        # Step 1 — is the app config present?
        configured = await _is_configured(uid)
        if configured == RC_NO_CLI:
            st.phase = "failed"
            st.error = f"lark-cli is not installed at {lark_bin()}"
            return
        if configured == RC_TIMEOUT:
            st.phase = "failed"
            st.error = "config show: probe timeout"
            return
        if configured != 0:
            # Step 2 — create an app. Blocks on the browser; URL comes off the
            # live stderr stream.
            rc, log = await _run_streaming_url(
                uid, ["config", "init", "--new"], st, INTERACTIVE_TIMEOUT
            )
            st.log = _clip(_redact_text(log))
            if rc != 0:
                st.phase = "failed"
                st.error = _err_summary(rc, log, "config init")
                return

        # Step 3 — initiate device authorization, non-blocking.
        st.phase = "starting" if not st.verify_url else st.phase
        rc, out, err = await _run(
            uid,
            [
                "auth", "login",
                "--recommend",
                "--domain", LOGIN_DOMAINS,
                "--no-wait",
                "--json",
            ],
            LOGIN_INIT_TIMEOUT,
        )
        st.log = _clip(_redact_text(err or out))
        if rc != 0:
            st.phase = "failed"
            st.error = _err_summary(rc, err or out, "auth login")
            return

        doc = _parse_json(err) or _parse_json(out)
        url = _find_key(
            doc, ("verification_url", "verification_uri_complete", "verification_uri")
        )
        code = _find_key(doc, ("device_code",))
        if not url or not code:
            st.phase = "failed"
            st.error = "auth login: response missing verification_url / device_code"
            return
        st.verify_url = str(url)
        st.phase = "await_verify"

        # Step 4 — poll until the user authorizes. `verify_url` deliberately
        # stays populated so the UI can keep showing the link/QR while polling.
        st.phase = "polling"
        rc, out, err = await _run(
            uid,
            ["auth", "login", "--device-code", str(code), "--json"],
            INTERACTIVE_TIMEOUT,
        )
        st.log = _clip(_redact_text(err or out))
        if rc != 0:
            st.phase = "failed"
            st.error = _err_summary(rc, err or out, "auth login --device-code")
            return

        st.identity = await _whoami(uid)
        st.phase = "bound"
        st.verify_url = ""
        st.error = ""
        # Fire-and-forget: schedule after `phase` is already "bound", so this
        # can never delay or fail the binding response itself.
        st.sync_task = asyncio.create_task(_sync_skills_after_bind(uid))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        _LOG.exception("lark binding flow failed for %s", uid)
        st.phase = "failed"
        st.error = f"internal error: {exc}"


async def start(uid: str) -> dict:
    """Kick off (or report) the binding flow. Idempotent while in flight."""
    async with _LOCK:
        st = _state(uid)
        if st.task and not st.task.done():
            return st.snapshot()
        if st.phase == "bound":
            return st.snapshot()
        st.phase = "starting"
        st.verify_url = ""
        st.identity = None
        st.error = ""
        st.log = ""
        st.task = asyncio.create_task(_flow(uid, st))
        return st.snapshot()


async def status(uid: str) -> dict:
    """Current state. Terminal phases are re-probed against the real CLI."""
    st = _STATES.get(uid)
    if st is not None and st.task and not st.task.done():
        return st.snapshot()

    if st is not None and st.phase not in ("unbound", "bound"):
        return st.snapshot()

    identity = await _whoami(uid)

    # Re-check after the await: a concurrent start() (or unbind()) may have
    # taken ownership while the probe was in flight. Writing unconditionally
    # here would stomp a freshly-set `starting` back to `unbound`, and the UI
    # would show "not bound" for a flow that is actually running.
    st = _STATES.get(uid)
    if st is not None and st.task and not st.task.done():
        return st.snapshot()
    if st is not None and st.phase not in ("unbound", "bound"):
        return st.snapshot()

    st = _state(uid)
    if identity is not None:
        st.phase = "bound"
        st.identity = identity
        st.verify_url = ""
        st.error = ""
    else:
        st.phase = "unbound"
        st.identity = None
        st.verify_url = ""
    return st.snapshot()


async def unbind(uid: str) -> None:
    """Cancel any in-flight flow, log out (tolerantly) and wipe CLI state.

    Holds `_LOCK` for the *whole* teardown. An earlier version released it
    around the cancel/logout/rmtree, which let a concurrent start() install a
    new task that the final state reset then dropped on the floor — an orphan
    flow still running against a user who believes they are unbound. Blocking
    start() until teardown finishes keeps the semantics idempotent.
    """
    async with _LOCK:
        st = _state(uid)
        task = st.task
        st.task = None

        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - flow already logs its own
                _LOG.warning("lark flow errored during unbind for %s", uid, exc_info=True)

        sync_task = st.sync_task
        st.sync_task = None

        if sync_task and not sync_task.done():
            # Must be cancelled *before* remove_all runs below: otherwise a
            # still-running sync() can `_post_install` a skill back in right
            # after remove_all just deleted it -- reinstating a registration
            # for a user who is in the middle of being unbound.
            sync_task.cancel()
            try:
                await sync_task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - sync already logs its own
                _LOG.warning("lark skills sync errored during unbind for %s", uid, exc_info=True)

        try:
            await _run(uid, ["auth", "logout"], LOGOUT_TIMEOUT)
        except Exception:  # pragma: no cover - logout is best-effort
            _LOG.warning("lark auth logout failed for %s", uid, exc_info=True)

        try:
            from lark import skills_sync

            await skills_sync.remove_all(uid)
        except Exception:  # pragma: no cover - removal is best-effort
            _LOG.warning("lark skills remove_all failed during unbind for %s", uid, exc_info=True)

        try:
            shutil.rmtree(user_home(uid) / ".lark-cli", ignore_errors=True)
        except Exception:  # pragma: no cover
            _LOG.warning("lark state cleanup failed for %s", uid, exc_info=True)

        _STATES[uid] = _State()
