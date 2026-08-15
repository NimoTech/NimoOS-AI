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

_URL_RE = re.compile(r"https://\S+")

# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


class _State:
    __slots__ = ("phase", "verify_url", "identity", "error", "log", "task")

    def __init__(self) -> None:
        self.phase: str = "unbound"
        self.verify_url: str = ""
        self.identity: dict | None = None
        self.error: str = ""
        self.log: str = ""
        self.task: asyncio.Task | None = None

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
    """Drop all in-memory state (test hook)."""
    for st in _STATES.values():
        if st.task and not st.task.done():
            st.task.cancel()
    _STATES.clear()


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
    doc = _parse_json(stderr)
    if isinstance(doc, dict):
        err = doc.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("subtype") or err.get("type")
            if msg:
                return f"{step}: {msg}"
    tail = (stderr or "").strip().splitlines()
    if tail:
        return f"{step}: {tail[-1][:400]}"
    return f"{step}: exit {rc}"


async def _run(uid: str, args: list[str], timeout: float) -> tuple[int, str, str]:
    """Run the CLI, capturing both streams. Returns (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        lark_bin(),
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_env(uid),
        cwd=str(user_home(uid)),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"timed out after {timeout}s"
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
    proc = await asyncio.create_subprocess_exec(
        lark_bin(),
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_env(uid),
        cwd=str(user_home(uid)),
    )
    buf: list[str] = []

    async def _pump(stream, scrape: bool):
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace")
            buf.append(text)
            st.log = _clip("".join(buf))
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
        return 124, "".join(buf) + f"\ntimed out after {timeout}s"
    except asyncio.CancelledError:
        proc.kill()
        raise
    return rc, "".join(buf)


# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------


async def _is_configured(uid: str) -> bool:
    rc, _out, _err = await _run(uid, ["config", "show"], PROBE_TIMEOUT)
    return rc == 0


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
    return doc


# ---------------------------------------------------------------------------
# state machine
# ---------------------------------------------------------------------------


async def _flow(uid: str, st: _State) -> None:
    try:
        # Step 1 — is the app config present?
        if not await _is_configured(uid):
            # Step 2 — create an app. Blocks on the browser; URL comes off the
            # live stderr stream.
            rc, log = await _run_streaming_url(
                uid, ["config", "init", "--new"], st, INTERACTIVE_TIMEOUT
            )
            st.log = _clip(log)
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
        st.log = _clip(err or out)
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
        st.log = _clip(err or out)
        if rc != 0:
            st.phase = "failed"
            st.error = _err_summary(rc, err or out, "auth login --device-code")
            return

        st.identity = await _whoami(uid)
        st.phase = "bound"
        st.verify_url = ""
        st.error = ""
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

    if st is None or st.phase in ("unbound", "bound"):
        identity = await _whoami(uid)
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
    """Cancel any in-flight flow, log out (tolerantly) and wipe CLI state."""
    async with _LOCK:
        st = _state(uid)
        task = st.task
        st.task = None
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    try:
        await _run(uid, ["auth", "logout"], LOGOUT_TIMEOUT)
    except Exception:  # pragma: no cover - logout is best-effort
        _LOG.warning("lark auth logout failed for %s", uid, exc_info=True)

    try:
        shutil.rmtree(user_home(uid) / ".lark-cli", ignore_errors=True)
    except Exception:  # pragma: no cover
        _LOG.warning("lark state cleanup failed for %s", uid, exc_info=True)

    async with _LOCK:
        _STATES[uid] = _State()
