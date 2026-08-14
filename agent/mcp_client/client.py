"""Wrap MCP tools as confirm-gated, blacklist-gated FunctionTools."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import OrderedDict
from contextvars import ContextVar
from contextlib import AsyncExitStack

import anyio
from agents import FunctionTool
from mcp.client import Client, InputRequiredRoundsExceededError
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_REQUEST, METHOD_NOT_FOUND
from mcp_types.jsonrpc import MISSING_REQUIRED_CLIENT_CAPABILITY, URL_ELICITATION_REQUIRED

from mcp_client.schema import sanitize_schema, flatten_result
from mcp_client.elicitation import make_elicitation_callback
from mcp_client.status import OK, FAILED, WARMING, CONFIG_ERROR, ServerStatus
from mcp_client.hashing import schema_hash, desc_hash

# Nominal connect budget; raised 5→8 for mcp 2.0 (mode="auto" first probes
# server/discover and falls back to the legacy initialize handshake on old
# servers, i.e. one extra round trip before we can list anything).
#
# CURRENTLY INERT as a runtime bound, though: it only flows into
# _build_transport's `connect_to` parameter, and that parameter is only
# consumed by the stdio branch (netns_client.start_mcp_stdio(connect_timeout=
# connect_to)) — the http branch builds its httpx2.AsyncClient with
# `timeout=session_to`, and the sse branch passes `timeout=session_to` to
# sse_client, neither touches connect_to. So today the 5→8 change has zero
# runtime effect; the actual upper bounds are: http/sse's run-start cold path
# is capped at the _metas_for_server layer by MCP_COLD_TOTAL_TIMEOUT, and
# stdio is capped by STDIO_CONNECT_TIMEOUT (via _connect_timeout()), not this
# constant. Not deleted — tests/test_mcp_stdio.py pins _connect_timeout()'s
# lookup table against it, and it is still the value _cold_fetch's default
# `connect_timeout=None` resolves to.
MCP_CONNECT_TIMEOUT = 8

# Single hard cap on the WHOLE run-start cold path (connect + list). Without it,
# raising the connect leg to 8s would push the worst case from 5+5 to 8+8 and make
# run start noticeably slower; this keeps it exactly where it was.
MCP_COLD_TOTAL_TIMEOUT = 10

# ClientSession read timeout — bounds each JSON-RPC request (list_tools AND call_tool),
# NOT just the connect. Must be generous: remote tool calls (e.g. MS Learn semantic
# search) routinely take several seconds, far past the 8s connect cap. call_tool has no
# outer wait_for, so it is bounded ONLY by this value — too small here silently cancels
# every slow tool call mid-flight (surfaces as httpx.ConnectTimeout/CancelledError).
MCP_SESSION_TIMEOUT = 60  # seconds

# sse 传输的空闲读超时。SDK 默认 300s（sse_client 的 sse_read_timeout 参数默认值，
# 由 tests/test_mcp_transport_timeouts.py 钉住）。
#
# 为什么要显式覆盖：一次 elicitation 期间链路上没有任何 in-flight 请求 —— 服务端已经
# 用完整的 InputRequiredResult 答复过了，我们在自己的回调里等用户。MCP_SESSION_TIMEOUT
# 那 60 秒是**每个请求**的钟，此刻不计时；唯一还在跑的就是这个空闲钟。让它取一个我们
# 不控制的 SDK 默认值，等于把 URL_ELICIT_WAIT（180s）能不能活下来交给运气。
#
# 900s 覆盖的是"用户去授权几分钟就回来"。它**不**覆盖表单卡的 24 小时
# DEFAULT_TIMEOUT —— 那个上限在 sse 传输上今天就已经不成立（既有限制，不是本次引入），
# 修法是重连后重发原调用，不是把这个数字调到 86400。
MCP_SSE_READ_TIMEOUT = 900

STDIO_CONNECT_TIMEOUT = 90  # seconds; the first stdio npx/uvx package fetch can be slow (cached locally after, then fast)

# ── stdio command allow-list (2026-07-16 hardening) ───────────────────────────
# A registered stdio MCP server spawns command+args directly in the netns
# executor, bypassing the shell guard. Without this, a user tricked into
# approving `add_mcp_server("bash -c 'rm -rf /DATA'")` would run an
# arbitrary destructive command on the next turn. Deny-by-default by BASENAME:
# only known MCP launchers may spawn, at any path (`/usr/bin/npx` ok). A path
# allow-by-directory rule was rejected — /bin, /usr/bin contain bash/rm/dd, so
# "any binary in a standard bin dir" would defeat the gate. Servers shipped as
# a bare binary must be launched via a launcher (uvx/npx/python -m …).
_MCP_STDIO_ALLOWED_CMDS = {
    "npx", "uvx", "uv", "node", "nodejs", "python", "python3",
    "deno", "bun", "bunx",
}


class McpCommandNotAllowed(Exception):
    """A stdio MCP server's launch command is not on the allow-list."""


def _assert_stdio_command_allowed(command: str) -> None:
    cmd = (command or "").strip()
    if not cmd:
        raise McpCommandNotAllowed("empty MCP stdio command")
    if os.path.basename(cmd) in _MCP_STDIO_ALLOWED_CMDS:
        return
    raise McpCommandNotAllowed(
        f"MCP stdio launch command not allowed: {cmd!r}. Launch the server via "
        f"an MCP launcher (npx/uvx/uv/node/python …).")

# runtime vars passed through to the stdio subprocess (missing these causes mojibake/timezone/tmpdir errors)
_ENV_PASSTHROUGH = ("LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR")


def _connect_timeout(server: dict) -> int:
    return STDIO_CONNECT_TIMEOUT if server.get("transport") == "stdio" else MCP_CONNECT_TIMEOUT


def _session_timeout(server: dict) -> int:
    # Per-request (list/call) read timeout. stdio reuses its generous connect budget
    # (local subprocess); http/sse must be generous for slow remote tool calls.
    return STDIO_CONNECT_TIMEOUT if server.get("transport") == "stdio" else MCP_SESSION_TIMEOUT


def _stdio_env(user_env: dict) -> dict:
    """Subprocess env = allow-listed passthrough ⊕ user env ⊕ protected core vars
    (core applied last, not overridable by the user).
    Does not inherit os.environ wholesale, to avoid leaking the agent’s sensitive vars."""
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    env.update(user_env or {})
    core = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("NIMOOS_MCP_HOME") or os.environ.get("HOME") or "/tmp",
        "npm_config_cache": os.environ.get("npm_config_cache", ""),
        "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", ""),
    }
    env.update({k: v for k, v in core.items() if v})
    return env


# Default entry lifetime when the server gives no hint (i.e. every legacy-protocol
# server, and any 2026-07-28 server that leaves ttlMs at its default 0). Past this
# an entry goes stale (still usable, triggers a background revalidate).
SCHEMA_TTL = 600
# Floor for a server-declared ttlMs. A server declaring something tiny would punch
# through the zero-connection warm path that is the whole reason this cache exists.
# Worst case is a manifest up to a minute stale — we explicitly do not subscribe to
# change notifications anyway, so that window is already accepted. Config changes
# invalidate through Go re-probing and advancing listed_at (the sole freshness
# authority — see _CacheEntry), unaffected by this floor.
SCHEMA_TTL_MIN = 60
# Ceiling for a server-declared ttlMs. Without it a server declaring 24h really
# gets cached for 24h, which multiplies the staleness window of removed tools
# (defect ①) — the unknown-tool invalidation self-heals mid-TTL, but only after
# the model has already tripped over the missing tool once.
SCHEMA_TTL_MAX = 3600
SCHEMA_CACHE_MAX = 256  # LRU capacity cap, bounds memory use

# MRTR round cap = the SDK default (mcp/client/_input_required.py::
# DEFAULT_INPUT_REQUIRED_MAX_ROUNDS). A "round" is a REQUEST/RETRY round trip, not a
# question put to the user: the user pondering a card burns none of these — our
# callback simply hasn't returned yet and _dispatch_all keeps awaiting it, with the
# round counter frozen and only our own confirm timeout running (24h for a form card,
# elicitation.py::URL_ELICIT_WAIT = 180s for a URL authorization card).
#
# What DOES burn rounds is the state-only leg: an InputRequiredResult carrying only
# requestState and no inputRequests. The spec treats that as a first-class shape (a
# server MUST include at least one of the two) and it is how a server says "the
# out-of-band authorization you accepted hasn't landed yet, ask me again". The SDK
# answers it with a bare sleep — 50ms, doubling, capped at 250ms — and retries. So a
# cap of 3, as the phase-2 handoff originally suggested, is spent in 0.05+0.1+0.2 =
# 350ms and would throw InputRequiredRoundsExceededError at a perfectly compliant
# server. 10 buys 0.05+0.1+0.2+0.25*7 = 2.1s.
#
# 2.1s is NOT the out-of-band OAuth budget, and no longer needs to be. The wait for a
# third-party authorization happens INSIDE our elicitation callback, where no round cap
# applies (see elicitation.py::URL_ELICIT_WAIT). Phase 2 returned "accept" the moment
# the user consented to open the link, which is exactly why it always landed here with
# the login unfinished; that behaviour is gone — the card now sends "accept" only when
# the user comes back and says they finished, so the retry usually gets a terminal
# result in a single round.
#
# What the cap still governs is the legs that do not ask the user: the state-only
# polling ones above, and above all the TIMEOUT path. URL_ELICIT_WAIT expiring sends
# "accept" with no user answer behind it (see the on_timeout rationale in
# elicitation.py), so the server may still be unauthorized and answer with state-only
# rounds — 2.1s of them, then InputRequiredRoundsExceededError and the
# _rounds_exceeded_msg wording below. That is the accepted landing spot for a genuinely
# abandoned card, not a reason to raise this number: a bigger cap only lengthens a
# poll the user is no longer participating in.
MCP_INPUT_REQUIRED_ROUNDS = 10

# Upper bound on aclose(). aclose() shields itself from cancellation (see McpConn),
# which turns "remote hangs" from "gets cancelled" into "waits forever" — this caps
# that new risk. Shielding only blocks the OUTER cancel; this inner deadline still fires.
MCP_CLOSE_TIMEOUT = 5


def _resolve_ttl(raw_ttl_ms) -> int:
    """Server-declared ttlMs -> our cache entry lifetime, in seconds.

    "Absent" and "0" converge for free: legacy responses have no such field and the
    SDK model defaults it to 0, so neither needs special-casing. Applied once, at
    write time — see _cache_put.
    """
    if not isinstance(raw_ttl_ms, (int, float)) or raw_ttl_ms <= 0:
        return SCHEMA_TTL
    return min(max(int(raw_ttl_ms) // 1000, SCHEMA_TTL_MIN), SCHEMA_TTL_MAX)


class _CacheEntry:
    """In-process side-cache of one server's schema bodies.

    Deliberately does NOT carry ttl / fingerprint / fetched_at anymore: the
    single authority on freshness is the DB (mcp_server_runtime.listed_at /
    ttl_sec). Keeping a second set of freshness books here would let the two
    diverge — a process restart empties this cache while the DB is still
    inside its TTL, or the reverse. This is exactly the reasoning already
    written down where the SDK's own response caching was disabled (see the
    `cache=None` comment in _connect: "two caches would fight").
    """
    __slots__ = ("metas", "listed_at")

    def __init__(self, metas, listed_at):
        self.metas = metas          # list[dict]: {"name","description","input_schema"}
        self.listed_at = listed_at  # the DB's mcp_server_runtime.listed_at this body was fetched under


_SCHEMA_CACHE: "OrderedDict[int, _CacheEntry]" = OrderedDict()  # key = server["id"], LRU
_REVALIDATING: set = set()             # re-entrancy guard: only one background refresh per server at a time
_BACKGROUND_TASKS: set = set()         # strong refs so asyncio.create_task tasks aren’t GC’d


def _extract_meta(mcp_tool) -> dict:
    # mcp 2.0's mcp_types.Tool exposes the JSON Schema via the Python attribute
    # `input_schema` (snake_case) — `inputSchema` is only the wire-format alias,
    # NOT a Python attribute on the model (verified empirically: getattr(tool,
    # "inputSchema", ...) returns the default on a real Tool instance). No
    # camelCase fallback here on purpose: a fallback that's never exercised by a
    # real Tool would silently keep working even if this line regressed back to
    # the wrong name — see test_extract_meta_real_tool, which uses a real
    # mcp_types.Tool precisely so that regression can't hide.
    return {"name": mcp_tool.name,
            "description": getattr(mcp_tool, "description", "") or "",
            "input_schema": getattr(mcp_tool, "input_schema", None)}


def _cache_put(server_id: int, metas, listed_at: int) -> None:
    """Write metas into the side cache, keyed by the listed_at they were
    fetched under.

    listed_at == 0 means the caller could not establish trust in this body
    (see mcp_client.runtime.fetch_schemas, which degrades to (0, []) on any
    network/parse failure) and MUST be treated as "nothing to cache", never
    as a new cache state — writing it would let one failed fetch silently
    blank out a perfectly good previously-cached manifest. So this is a
    strict no-op when listed_at is 0: whatever was cached before (if
    anything) is left exactly as it was.
    """
    if listed_at == 0:
        return
    _SCHEMA_CACHE[server_id] = _CacheEntry(metas, listed_at)
    _SCHEMA_CACHE.move_to_end(server_id)
    while len(_SCHEMA_CACHE) > SCHEMA_CACHE_MAX:
        _SCHEMA_CACHE.popitem(last=False)   # evict least-recently-used


def _cache_get(server_id: int, listed_at: int):
    """Return the cached entry iff it was cached under exactly this
    listed_at; otherwise None. A mismatch (older, newer, or simply no entry)
    is always a miss — the DB is the sole authority on which listed_at is
    current, so this cache never guesses about freshness on its own."""
    entry = _SCHEMA_CACHE.get(server_id)
    if entry is None or entry.listed_at != listed_at:
        return None
    _SCHEMA_CACHE.move_to_end(server_id)
    return entry


# Set per-run by agent.py (mirrors how skills/* receive context).
SESSION_ID_VAR: ContextVar = ContextVar("mcp_session_id", default="")
EVENT_QUEUE_VAR: ContextVar = ContextVar("mcp_event_queue", default=None)
CONFIRM_MGR_VAR: ContextVar = ContextVar("mcp_confirm_mgr", default=None)
USER_PATTERNS_VAR: ContextVar = ContextVar("mcp_user_patterns", default=[])
# session-scoped set of "serverid::toolname" the user chose to remember.
# CONTRACT: agent.py MUST call _CONFIRMED_TOOLS_VAR.set(set()) at the start of
# each run (see Task 13) to prevent cross-run/session bleed of approvals.
_CONFIRMED_TOOLS_VAR: ContextVar = ContextVar("mcp_confirmed_tools", default=set())

# Per-run lazy MCP connections. agent.py MUST set both to fresh {} at run start.
_RUN_CONNS_VAR: ContextVar = ContextVar("mcp_run_conns", default=None)
_RUN_CONN_LOCKS_VAR: ContextVar = ContextVar("mcp_run_conn_locks", default=None)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_PATH_KEY_RE = re.compile(r"(path|file|dir|directory)", re.IGNORECASE)


def _slug(name: str) -> str:
    return _SLUG_RE.sub("_", name.strip().lower()).strip("_") or "server"


def _gate_args(args: dict, patterns: list[str]) -> None:
    """Heuristic hard-blacklist gate on path-like arguments. Raises ValueError
    when a value matches a user blacklist pattern."""
    if not patterns:
        return
    for k, v in args.items():
        if not isinstance(v, str):
            continue
        looks_pathy = _PATH_KEY_RE.search(k) or v.startswith("/")
        if not looks_pathy:
            continue
        for pat in patterns:
            if pat and pat in v:
                raise ValueError(f"argument {k} hit the blacklist ({pat})")


async def _ensure_confirmed(server: dict, tool_name: str, args: dict) -> bool:
    key = f"{server['id']}::{tool_name}"
    if key in _CONFIRMED_TOOLS_VAR.get(set()):
        return True
    mgr = CONFIRM_MGR_VAR.get()
    queue = EVENT_QUEUE_VAR.get()
    session_id = SESSION_ID_VAR.get()
    confirm_id = mgr.register(
        session_id, f"mcp_call:{key}",
        f'Call tool {tool_name} on MCP server "{server["name"]}"',
        json.dumps(args, ensure_ascii=False)[:500])
    await queue.put({
        "type": "confirmation_required", "confirm_id": confirm_id,
        "kind": "mcp_tool", "server": server["name"], "tool": tool_name,
        "remember_scope": "tool",
    })
    confirmed = await mgr.wait(confirm_id)
    if confirmed and mgr.consume_remember(confirm_id):
        s = _CONFIRMED_TOOLS_VAR.get(set())
        s.add(key)
        _CONFIRMED_TOOLS_VAR.set(s)
    return confirmed


def _rounds_exceeded_msg(server_name: str) -> str:
    """MRTR rounds exhausted AFTER we already put the question to the user.

    Phase 1 reused the "capability not supported" wording here, and in phase 2 that
    is simply false: elicitation IS supported now. The real meaning is "we asked, we
    answered the server, it still isn't ready" — overwhelmingly an out-of-band
    authorization that hasn't completed. Telling the model to check the server's
    CONFIGURATION would send the user off fixing the wrong thing.

    The premise is deliberately vaguer than "the user answered", because it has to
    cover the URL card's timeout path too: there, URL_ELICIT_WAIT expired and we sent
    `accept` with no user answer behind it (see elicitation.py). Both paths land here
    for the same underlying reason and want the same advice, so only the actionable
    half is stated as fact.
    """
    return (f'[MCP error] MCP server "{server_name}" asked for input, we responded, '
            "and the server is still not ready. This is almost always an out-of-band "
            "authorization that has not finished yet — it is NOT a problem with the call "
            "arguments, so do NOT retry with different arguments. Tell the user to finish "
            "the authorization in the page that was opened, then ask you to retry.")


def _unsupported_capability_msg(server_name: str) -> str:
    """A capability we deliberately never declare (sampling / roots) was required.

    Wording is phase 1's, unchanged and still accurate for those two: we really do not
    support them, and the server really does need reconfiguring. Only the elicitation
    half of the old shared message moved out (see _rounds_exceeded_msg).
    """
    return (f'[MCP error] MCP server "{server_name}" needs interactive input to complete '
            "this call, which is not supported. This is a server-side capability issue "
            "unrelated to the call arguments — do NOT retry with different arguments; "
            "tell the user to check that MCP server's configuration.")


def _legacy_url_elicitation_msg(server_name: str) -> str:
    """The 2025-11-25 URL-authorization flow, which we deliberately do not implement.

    Elicitation is two entirely different wire mechanisms across the two protocol
    versions. Under 2026-07-28 a URL request rides inside `InputRequiredResult`.
    Under 2025-11-25 it is an ERROR (-32042) carrying `data.elicitations`, and the
    completion signal is a `notifications/elicitation/complete` naming an
    `elicitationId` — a notification the client side of the SDK has no handler for at
    all (grep over mcp/client/ and mcp/shared/ finds nothing; only mcp/server/* sends
    it). We could show the card, but we could never learn that the user finished.

    We reach here at all because _connect uses mode="auto", so a legacy session is a
    live possibility. Phase 1 let this fall into the generic "[MCP error] ... failed"
    branch, which told the user nothing. Being unsupported is fine; being unsupported
    silently is not.
    """
    return (f'[MCP error] MCP server "{server_name}" is asking for authorization using '
            "the legacy (2025-11-25) URL elicitation flow, which this client does not "
            "support. This is a protocol-version issue unrelated to the call arguments "
            "— do NOT retry with different arguments; tell the user this server needs "
            "to support MCP 2026-07-28 for authorization to work.")


def _is_legacy_url_elicitation(err) -> bool:
    """True for URL_ELICITATION_REQUIRED (-32042), the 2025-11-25-only shape.

    Code alone is a sufficient discriminator — it is a dedicated code with exactly this
    meaning, mirroring how _is_unsupported_capability treats -32021.
    """
    data = getattr(err, "error", None)
    return data is not None and getattr(data, "code", None) == URL_ELICITATION_REQUIRED


def _is_unsupported_capability(err) -> bool:
    """True when a tool call cannot proceed because it needs a client capability we
    deliberately never declare (sampling / roots).

    Elicitation is NOT in that set as of phase 2 — `_connect` now passes an
    `elicitation_callback`, so a compliant server asking for it succeeds via the normal
    MRTR path instead of landing here. This function still matters for elicitation in
    one narrow case: a legacy/non-compliant server that sends `inputRequests` in a shape
    our declared capability doesn't cover, or before capability negotiation settles.

    Two wire shapes mean "capability not supported", and BOTH must be recognised — they
    come from opposite kinds of server:

    1. `MISSING_REQUIRED_CLIENT_CAPABILITY` (-32021) — how a **compliant** 2026-07-28
       server says "this call needs a capability you did not declare", carrying
       `data.requiredCapabilities`. This is the case we actually meet in the field, and
       the code alone is a sufficient discriminator: it is a dedicated code with exactly
       this meaning.
    2. `INVALID_REQUEST` (-32600) + a message ending in "not supported" — produced by the
       SDK's own built-in default callbacks when a **non-compliant** server sends
       `inputRequests` to a client that never declared the capability. This shape no
       longer applies to elicitation (we declare it, so the SDK's default callback is
       never in play for it) — it now only fires for sampling/roots. Here the code is
       generic, so the message suffix is needed to narrow the false-positive surface;
       tests/test_mcp_mrtr.py pins the SDK's three sentinel strings so a rewording fails
       loudly instead of silently degrading to the generic error path.

    In both cases a remote tool's ordinary business failure is NOT at risk of being
    misread: those arrive as `isError=True` inside a result, never as a JSON-RPC error.
    """
    data = getattr(err, "error", None)
    if data is None:
        return False
    code = getattr(data, "code", None)
    if code == MISSING_REQUIRED_CLIENT_CAPABILITY:
        return True
    return (code == INVALID_REQUEST
            and str(getattr(data, "message", "")).endswith("not supported"))


_UNKNOWN_TOOL_RE = re.compile(
    r"unknown tool"
    r"|no such tool"
    r"|tool\s+[\"'`]?[\w./-]+[\"'`]?\s+(not found|does not exist)",
    re.IGNORECASE)


def _is_unknown_tool(err) -> bool:
    """True when a call failed because the server no longer has the tool.

    Deliberately narrow — ONLY this signature may drop the schema cache;
    invalidating on any MCPError would let plain argument errors punch through
    the warm path the cache exists to provide (defect-① review note). Two
    shapes are recognised: JSON-RPC METHOD_NOT_FOUND (-32601), and the common
    "Unknown tool …" / "no such tool" / "tool <name> not found" wordings
    servers put on generic codes. The tool-name form requires the name to sit
    immediately next to "not found"/"does not exist" so that unrelated
    resource errors merely mentioning a tool in passing (e.g. "Error
    executing tool read_file: File not found") are not misclassified as a
    missing tool — that would drop the schema cache and tell the model not to
    retry, when a corrected argument is the right recovery.
    """
    data = getattr(err, "error", None)
    if data is None:
        return False
    if getattr(data, "code", None) == METHOD_NOT_FOUND:
        return True
    return bool(_UNKNOWN_TOOL_RE.search(str(getattr(data, "message", ""))))


def _wrap_tool(server: dict, meta: dict, slug: str = None) -> FunctionTool:
    """slug is optional so every existing single-server call site (tests that
    exercise one server in isolation) keeps working unchanged: it falls back to
    slugging this server's own name, exactly what assign_slugs would produce
    for a lone server anyway. build_mcp_tools is the one caller that MUST pass
    the already-deduped slug — see assign_slugs — so that two servers sharing
    a slug don't both claim the bare `mcp__<slug>__` prefix."""
    if slug is None:
        slug = _slug(server["name"])
    tool_name = meta["name"]
    fq_name = f"mcp__{slug}__{tool_name}"
    schema = sanitize_schema(meta.get("input_schema"))

    async def _on_invoke(ctx, input_json: str) -> str:
        try:
            args = json.loads(input_json or "{}")
        except Exception:
            args = {}
        try:
            _gate_args(args, USER_PATTERNS_VAR.get([]))
        except ValueError as e:
            return f"[MCP error] blocked by blacklist: {e}"
        if not await _ensure_confirmed(server, tool_name, args):
            return "[MCP error] the user denied this MCP tool call"
        try:
            conn = await _get_run_conn(server)          # lazy connect (connection layer)
        except Exception as e:
            return (f'[MCP error] cannot connect to MCP server "{server["name"]}" ({e}). '
                    "This is a connection/server failure unrelated to the call arguments — "
                    "do NOT retry with different arguments; tell the user to check that MCP server.")
        try:
            result = await conn.call_tool(tool_name, args)   # tool execution layer
        except InputRequiredRoundsExceededError:
            return _rounds_exceeded_msg(server["name"])
        except MCPError as e:
            if _is_legacy_url_elicitation(e):
                return _legacy_url_elicitation_msg(server["name"])
            if _is_unsupported_capability(e):
                return _unsupported_capability_msg(server["name"])
            if _is_unknown_tool(e):
                # Defect ①: within the TTL the manifest keeps advertising a tool
                # the server has removed, and nothing ever corrected it. Drop the
                # entry and refresh in the background. The run's tool set is
                # immutable (an SDK decision), so the refresh helps the NEXT run
                # — the message states that honestly.
                _SCHEMA_CACHE.pop(server["id"], None)
                _schedule_revalidate(server)
                return (f'[MCP error] MCP server "{server["name"]}" no longer recognizes '
                        f"tool {tool_name} — it may have been removed on the server side. "
                        "The tool list will be refreshed for your next message; do NOT "
                        "retry this call with different arguments.")
            return f"[MCP error] MCP tool {tool_name} failed: {e}"
        except Exception as e:
            return f"[MCP error] MCP tool {tool_name} failed: {e}"
        return flatten_result(result)

    return FunctionTool(
        name=fq_name,
        description=meta.get("description", "") or "",
        params_json_schema=schema,
        on_invoke_tool=_on_invoke,
        strict_json_schema=False,
    )


class McpConn:
    """Holds a credential-configured MCP client instance for one run.

    There is NO protocol session under MCP 2026-07-28. The lifecycle managed here
    exists for two unrelated reasons:
      (a) stdio: a real local subprocess needs someone to start and kill it;
      (b) http/sse: the SDK takes user auth headers on the pre-built httpx client,
          not per call — so the credentials need somewhere to live.
    It is not protocol state, and TCP reuse is only an incidental benefit.
    """
    __slots__ = ("server", "client", "stack")

    def __init__(self, server: dict, client, stack):
        self.server = server
        self.client = client
        self.stack = stack

    async def call_tool(self, name: str, args: dict):
        return await self.client.call_tool(name, args)

    async def list_tools(self) -> tuple[list[dict], int]:
        """Return (tool metas, cache ttl in seconds) — exactly what the schema
        cache stores, so callers never touch the SDK result object.

        cacheScope is deliberately NOT read: it constrains shared intermediary
        proxies, and we are an end client — storing a field we never act on would
        only make a later reader guess what it is for.
        """
        result = await self.client.list_tools()
        metas = [_extract_meta(t) for t in result.tools]
        return metas, _resolve_ttl(getattr(result, "ttl_ms", None))

    async def aclose(self):
        try:
            # Shielded: _cold_fetch's finally and test_server both call this from
            # inside an outer asyncio.wait_for. A bare await would be re-cancelled
            # on the spot, leaving the Client context unexited (leaked httpx client
            # / unix socket). move_on_after keeps the shield from waiting forever.
            with anyio.CancelScope(shield=True):
                with anyio.move_on_after(MCP_CLOSE_TIMEOUT):
                    await self.stack.aclose()
        except Exception:
            pass

    # --- McpConn method, same pattern as list_tools: keep the SDK result objects away
    #     from callers ---
    def protocol_info(self) -> dict:
        """What the connect-time negotiation settled on. Synchronous: it only reads
        attributes Client.__aenter__ already populated, so there is no I/O.

        The era discriminator is `discover_result is not None`, and it is authoritative:
        mcp/client/_probe.py::negotiate_auto calls session.adopt(result) -- the one
        place that sets _discover_result (session.py:661) -- only on the modern path,
        while every fallback path goes through session.initialize(), which clears the
        field back to None (session.py:668). "The server answered discover but
        advertised no modern version" also takes a fallback branch, so it reads None
        too. That is why we need no version table of our own here: no import of
        mcp_types.version, no string comparison, no date ordering.
        """
        session = self.client.session
        dr = session.discover_result
        version = self.client.protocol_version
        if dr is not None:
            return {"protocol_era": "modern", "protocol_version": version,
                    "supported_versions": list(dr.supported_versions)}
        # Legacy has no enumeration primitive: initialize returns exactly one negotiated
        # revision. Reporting [version] is the whole truth available without
        # re-handshaking at each of the four handshake revisions. The UI must therefore
        # word this as "negotiated X", never "supports X".
        return {"protocol_era": "legacy", "protocol_version": version,
                "supported_versions": [version]}


# --- module level ---
def _protocol_fields(conn) -> dict:
    """A version readout must never turn a successful probe into a failed one."""
    try:
        return conn.protocol_info()
    except Exception:
        return {"protocol_era": "unknown", "protocol_version": None,
                "supported_versions": []}


def _read_instructions(conn) -> str:
    """Server self-description. Both protocol eras carry this field
    (DiscoverResult.instructions / InitializeResult.instructions); the spec
    explicitly suggests folding it into the system prompt."""
    try:
        session = conn.client.session
        for src in (session.discover_result, session.initialize_result):
            if src is not None and getattr(src, "instructions", None):
                return str(src.instructions)
    except Exception:
        pass
    return ""


def _read_server_info(conn) -> dict:
    """serverInfo. On the modern path it comes from the discover result's _meta
    stamp; on legacy it comes from the initialize result. The SDK's
    session.server_info already unifies both paths (mcp/client/session.py:777-788)."""
    try:
        info = conn.client.session.server_info
        if info is None:
            return {}
        return {
            "name": getattr(info, "name", "") or "",
            "title": getattr(info, "title", "") or "",
            "version": getattr(info, "version", "") or "",
            "description": getattr(info, "description", "") or "",
        }
    except Exception:
        return {}


async def _emit_warning(server_name: str, err) -> None:
    queue = EVENT_QUEUE_VAR.get()
    if queue is None:
        return
    await queue.put({"type": "mcp_warning", "server": server_name, "error": str(err)})


async def _build_transport(server: dict, stack: AsyncExitStack,
                           connect_to: int, session_to: int):
    """Pick and construct the SDK transport for this server. Anything needing
    explicit teardown is registered on *stack* so a failed connect still releases it.

    NOTE (deviation from the task-3 brief, verified empirically — see
    task-3-report.md): the brief's draft passes `http_client=` to BOTH
    streamable_http_client and sse_client. That only exists on
    streamable_http_client — sse_client's real signature is
    `sse_client(url, headers=None, timeout=5.0, sse_read_timeout=300.0,
    httpx_client_factory=..., auth=None, on_session_created=None)`, i.e. it takes
    headers/timeout directly and has no http_client= parameter at all; passing one
    would raise TypeError. So sse gets its auth headers via its own headers=
    kwarg instead of a pre-built httpx2 client — there is nothing of ours to
    register on stack for that branch.
    """
    transport = server.get("transport", "http")
    headers = server.get("headers", {}) or {}
    if transport == "http":
        import httpx2
        # The user's auth headers can ONLY be supplied through a pre-built httpx
        # client here — streamable_http_client takes no headers= parameter.
        http_client = await stack.enter_async_context(
            httpx2.AsyncClient(headers=headers, timeout=session_to))
        return streamable_http_client(server["url"], http_client=http_client)
    if transport == "sse":
        return sse_client(server["url"], headers=headers, timeout=session_to,
                          sse_read_timeout=MCP_SSE_READ_TIMEOUT)
    if transport == "stdio":
        # Deny-by-default gate: the stdio command spawns directly in the netns
        # executor, bypassing the shell guard — never spawn an off-list command.
        _assert_stdio_command_allowed(server.get("command", ""))
        from mcp_client.netns_stdio import netns_stdio_transport
        import netns.client as netns_client
        socket_path = await netns_client.start_mcp_stdio(
            command=server["command"],
            args=server.get("args", []),
            env=_stdio_env(server.get("env", {})),
            connect_timeout=connect_to,
        )
        return netns_stdio_transport(socket_path)
    raise ValueError(f"unsupported transport: {transport}")

# NOTE (unchanged behaviour, called out so nobody "fixes" it here): start_mcp_stdio
# spawns the subprocess BEFORE the transport is opened, so if Client construction
# fails the subprocess is reclaimed by the netns executor's own timeout, not by us.
# That is exactly how it worked before this upgrade; changing it is out of scope.


async def _connect(server: dict, connect_timeout: int = None) -> "McpConn":
    """Build a transport, wrap it in an SDK Client, and hand back a McpConn whose
    aclose() unwinds the whole stack (Client → transport → socket/subprocess).

    connect_timeout is only actually ENFORCED for the stdio branch (passed straight
    through to netns start_mcp_stdio). For http/sse, the connect is bounded
    differently in different callers: _get_run_conn and _revalidate do NOT wrap
    _connect() in asyncio.wait_for, so the handshake is bounded only by the generous
    httpx2 AsyncClient(timeout=session_to) built in _build_transport. For the
    run-start cold path, _metas_for_server wraps _cold_fetch (which calls _connect)
    in asyncio.wait_for with MCP_COLD_TOTAL_TIMEOUT, capping the entire connect+list
    sequence together. test_server also wraps its connect+list probe in an outer
    asyncio.wait_for.
    """
    connect_to = connect_timeout if connect_timeout is not None else _connect_timeout(server)
    session_to = _session_timeout(server)

    stack = AsyncExitStack()
    try:
        transport = await _build_transport(server, stack, connect_to, session_to)
        client = await stack.enter_async_context(Client(
            transport,
            # mode="auto" IS the dual-protocol support: probe server/discover, fall
            # back to the legacy initialize handshake on old servers. We write no
            # protocol-version logic of our own.
            mode="auto",
            read_timeout_seconds=session_to,
            input_required_max_rounds=MCP_INPUT_REQUIRED_ROUNDS,
            # SDK-side response caching is off: this project's own manifest cache is
            # keyed on Go's DB-authoritative listed_at (see _CacheEntry), a freshness
            # semantic the SDK's own cache doesn't have, and two caches would fight.
            cache=None,
            # This single argument declares BOTH elicitation sub-capabilities.
            # mcp/client/session.py::_build_capabilities builds
            #   ElicitationCapability(form=FormElicitationCapability(),
            #                         url=UrlElicitationCapability())
            # unconditionally whenever the callback differs from the SDK default —
            # there is no form-only setting. And the spec says servers MUST NOT send a
            # mode the client did not declare, so declaring `url` obliges us to have a
            # url card. That is why the two cards shipped together rather than in two
            # phases. Pinned by test_mcp_protocol_compat.py::
            # test_we_declare_both_elicitation_modes_but_still_no_sampling_or_roots.
            #
            # Still NO sampling / roots callback, on purpose: sampling would let a
            # third-party server spend our model budget and inject prompts into our
            # model. Not declaring is the strongest defence — a compliant server never
            # asks — and _is_unsupported_capability still covers the rest.
            elicitation_callback=make_elicitation_callback(
                server,
                session_id_var=SESSION_ID_VAR,
                queue_var=EVENT_QUEUE_VAR,
                mgr_var=CONFIRM_MGR_VAR),
        ))
    except BaseException:
        # Shielded: the caller's asyncio.wait_for cancels us mid-handshake on timeout —
        # the single most common failure here. A bare await would be re-cancelled and
        # leak the unix socket or the stdio subprocess. move_on_after mirrors
        # McpConn.aclose(): shielding turns "remote hangs" from "gets cancelled" into
        # "waits forever" — this caps that new risk the same way aclose() does.
        with anyio.CancelScope(shield=True):
            with anyio.move_on_after(MCP_CLOSE_TIMEOUT):
                await stack.aclose()
        raise
    return McpConn(server=server, client=client, stack=stack)


async def _get_run_conn(server: dict) -> "McpConn":
    """Lazily connect on first use within a run; reuse for the rest of the run.
    A per-server lock makes concurrent tool calls share one connection."""
    conns = _RUN_CONNS_VAR.get()
    if conns is None:
        raise RuntimeError("_RUN_CONNS_VAR not initialised; agent.py must call .set({}) at run start")
    sid = server["id"]
    if sid in conns:
        return conns[sid]
    locks = _RUN_CONN_LOCKS_VAR.get()
    lock = locks.setdefault(sid, asyncio.Lock())
    async with lock:
        if sid in conns:                 # double-check after acquiring
            return conns[sid]
        # call-time: the user is actively invoking the tool — tolerate a cold/slow
        # connect (generous cap) instead of failing the call at the 8s run-start cap.
        conn = await _connect(server, connect_timeout=_session_timeout(server))
        conns[sid] = conn
        return conn


async def close_run_conns() -> None:
    conns = _RUN_CONNS_VAR.get() or {}
    for c in list(conns.values()):
        try:
            await c.aclose()
        except Exception:
            pass
    conns.clear()


def _schedule_revalidate(server: dict) -> None:
    sid = server["id"]
    if sid in _REVALIDATING:           # single-flight per server
        return
    _REVALIDATING.add(sid)
    task = asyncio.create_task(_revalidate(server))
    _BACKGROUND_TASKS.add(task)        # strong ref so the task isn't GC'd mid-await
    def _done(t):
        _BACKGROUND_TASKS.discard(t)
        _REVALIDATING.discard(sid)
    task.add_done_callback(_done)


async def _revalidate(server: dict) -> None:
    try:
        # background: not blocking any run, so use the generous session budget for
        # both connect and list (tolerates a cold remote connect that the 8s
        # run-start cap would reject). This is what self-heals a slow server.
        conn = await _connect(server, connect_timeout=_session_timeout(server))
        try:
            metas, _ttl = await asyncio.wait_for(conn.list_tools(),
                                                 timeout=_session_timeout(server))
            # listed_at, not the server's own ttlMs, is what this write is keyed
            # under — see _CacheEntry. server["listed_at"] is whatever Go handed
            # this run at start; if this server was never probed by Go (0), the
            # _cache_put guard below is a no-op rather than caching under "0".
            _cache_put(server["id"], metas, server.get("listed_at", 0))
        finally:
            await conn.aclose()
    except Exception:
        pass   # keep stale cache; background task must never raise


async def _cold_fetch(server: dict):
    """Connect once just to read schemas; cache + return metas. Connection is
    closed immediately (real calls use the per-run lazy connection)."""
    conn = await _connect(server)
    try:
        # No wait_for here — the caller (_metas_for_server) wraps the whole _cold_fetch
        # in asyncio.wait_for with MCP_COLD_TOTAL_TIMEOUT, capping the complete cold path
        # (connect + list). Wrapping again here with the same constant would create dead code:
        # the outer deadline (started before _connect) always fires first, so the inner
        # wait_for would never actually timeout. Per-request read timeouts are separately
        # bounded by Client(read_timeout_seconds=...) in _connect.
        metas, _ttl = await conn.list_tools()
    finally:
        await conn.aclose()
    _cache_put(server["id"], metas, server.get("listed_at", 0))
    return metas


async def _metas_for_server(server: dict):
    """Return (tool metas, status, detail) for a server, preferring cache.
    Cold / listed_at advanced -> fetch inline. On failure returns
    ([], FAILED/WARMING, reason) and still emits the UI warning event — the
    status return is the model-facing channel (defect 1), the event is the
    UI-facing one; both render the same fact.

    Freshness is a single exact match against server["listed_at"] — the value
    Go handed this run at start (see _CacheEntry). There is no separate
    stale-while-revalidate window here anymore: once Go's listed_at moves on
    (new probe, config change, anything), the old body is just a miss, not a
    "stale but good enough" body to keep serving.
    """
    listed_at = server.get("listed_at", 0)
    entry = _cache_get(server["id"], listed_at)
    if entry is not None:
        return entry.metas, OK, ""
    # cold / listed_at advanced since our last fetch:
    if server.get("transport") == "stdio":
        _schedule_revalidate(server)            # background single-flight self-healing warmup (connect+list+cache), does not block run startup
        await _emit_warning(server.get("name", "mcp"),
                            "stdio tools are initializing in the background for first use; retry shortly")
        return [], WARMING, "stdio server is initializing in the background"
    try:
        # ONE budget for the whole cold path (connect + list). The connect leg alone
        # is now 8s for mode="auto"'s extra server/discover round trip; without this
        # cap the run-start worst case would grow from 5+5 to 8+8.
        metas = await asyncio.wait_for(_cold_fetch(server), timeout=MCP_COLD_TOTAL_TIMEOUT)
        return metas, OK, ""
    except Exception as e:
        # A cold-fetch timeout is usually a cold connection on a slow link.
        # Warm the cache in the background with a generous timeout; tools are ready by the next run.
        _schedule_revalidate(server)
        await _emit_warning(server.get("name", "mcp"), e)
        return [], FAILED, str(e) or type(e).__name__


def assign_slugs(servers: list[dict]) -> dict[int, str]:
    """Resolve each server's stable slug for tool-name prefixing, deduping
    collisions in *servers* order: the first server to claim a slug keeps the
    bare form, later ones get `_2`, `_3`, ...

    Prefers the server's self-reported `handle` (Task 7, Go derives it from
    serverInfo.name / package name / URL host / typed name / command, in that
    order) over slugifying the user-typed `name`. This matters: Go and the
    model both speak in terms of `handle` (L1 tells the model "expand as:
    mcp:<handle>" — see Task 14/15). If this function slugged from `name`
    instead, a server whose typed name differs from its self-reported
    identity would get tools prefixed `mcp__<something-else>__`, and the gate
    the model was told to open would no longer correspond to any tool it can
    see. Falls back to `_slug(name)` only when there is no handle yet (e.g.
    the server has never been successfully probed).
    """
    slugs: dict[int, str] = {}
    seen: dict[str, int] = {}
    for s in servers:
        base = s.get("handle") or _slug(s.get("name", ""))
        count = seen.get(base, 0) + 1
        seen[base] = count
        slugs[s["id"]] = base if count == 1 else f"{base}_{count}"
    return slugs


async def build_mcp_tools(servers: list[dict]) -> tuple:
    """Build confirm/blacklist-gated FunctionTools for this run from the schema
    cache (zero connection when warm). Connections are established lazily per
    tool call (see _get_run_conn). Returns (flat FunctionTool list, per-server
    ServerStatus list in the same order as *servers*) — the status side is the
    defect-1 fix: load failures become visible to the model instead of only to
    the UI event stream."""
    probed = [s for s in servers if not s.get("config_error")]
    metas_per = await asyncio.gather(*[_metas_for_server(s) for s in probed],
                                     return_exceptions=True)
    results = iter(metas_per)
    # Dedup happens once, at the slug level, BEFORE any tool name is built —
    # not by patching individual tool names afterwards. Two servers that both
    # slug to "github" must become mcp__github__* and mcp__github_2__*, never
    # mcp__github__create_issue / mcp__github__create_issue_2: the latter
    # leaves the model unable to tell which server it's calling, and breaks
    # the correspondence between the gate a user opens (mcp:github_2) and the
    # tools that gate exposes.
    slugs = assign_slugs(servers)
    tools: list = []
    statuses: list = []
    for s in servers:
        name = s.get("name", "mcp")
        if s.get("config_error"):
            # Go flagged this server's stored credentials as undecryptable; do
            # not connect with an unauthenticated config — a 401 at call time
            # would mask the real cause.
            statuses.append(ServerStatus(name=name, status=CONFIG_ERROR,
                                         detail=str(s["config_error"])))
            continue
        res = next(results)
        if isinstance(res, Exception):
            await _emit_warning(name, res)
            statuses.append(ServerStatus(name=name, status=FAILED,
                                         detail=str(res) or type(res).__name__))
            continue
        metas, status, detail = res
        fq_names = []
        for meta in metas:
            tool = _wrap_tool(s, meta, slug=slugs[s["id"]])
            tools.append(tool)
            fq_names.append(tool.name)
        statuses.append(ServerStatus(name=name, status=status, detail=detail,
                                     tool_names=fq_names))
    return tools, statuses


# The probe budget is PER PHASE, not one flat ceiling. The connect phase has to
# accommodate the SDK's full two-stage negotiation: mode="auto" spends up to
# DISCOVER_TIMEOUT_SECONDS (10s, mcp/client/session.py:67) on server/discover and only
# falls back to the legacy initialize handshake after an MCPError -- which includes
# -32001, the probe's own timeout. One flat budget lets a stalled discover eat the
# whole probe and starve that fallback, and the fallback is exactly the case this
# feature has to report correctly.
PROBE_CONNECT_TIMEOUT = 20
STDIO_PROBE_CONNECT_TIMEOUT = 90    # the first npx/uvx package fetch dominates; process spawn does not
PROBE_LIST_TIMEOUT = 15
STDIO_PROBE_LIST_TIMEOUT = 20       # by now the subprocess is up; only tools/list is left

# Outer backstop. Must be >= connect + list + close, or it truncates a phase that is
# still inside its own budget. The close phase counts: _test_server_inner's
# `finally: await conn.aclose()` runs INSIDE this wait_for and is itself bounded by
# MCP_CLOSE_TIMEOUT (5s), so a hung teardown on an otherwise successful probe would
# eat the slack and surface as probe_timeout, discarding a result we already had.
# The backstop must also stay below the Go caller's timeout in route/v2/mcp.go
# (43s http / 125s stdio, route/v2/mcp.go:349), so Python cancels first and releases
# the subprocess and socket instead of Go abandoning a request that keeps running.
TEST_TIMEOUT = 41          # 20 + 15 + 5 (close) + 1
STDIO_TEST_TIMEOUT = 120   # 90 + 20 + 5 (close) + 5


def _probe_connect_timeout(server: dict) -> int:
    return STDIO_PROBE_CONNECT_TIMEOUT if server.get("transport") == "stdio" else PROBE_CONNECT_TIMEOUT


def _probe_list_timeout(server: dict) -> int:
    return STDIO_PROBE_LIST_TIMEOUT if server.get("transport") == "stdio" else PROBE_LIST_TIMEOUT


async def test_server(server: dict) -> dict:
    timeout = STDIO_TEST_TIMEOUT if server.get("transport") == "stdio" else TEST_TIMEOUT
    try:
        return await asyncio.wait_for(_test_server_inner(server), timeout=timeout)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Probe timed out", "error_key": "probe_timeout"}


async def _test_server_inner(server: dict) -> dict:
    connect_to = _probe_connect_timeout(server)
    list_to = _probe_list_timeout(server)
    try:
        # Both layers are needed: connect_timeout= is the only bound actually enforced
        # on the stdio branch (see _connect's docstring), while the surrounding wait_for
        # is the real ceiling for http/sse. Dropping either leaves an unbounded path.
        conn = await asyncio.wait_for(_connect(server, connect_timeout=connect_to),
                                      timeout=connect_to)
    except asyncio.TimeoutError:
        # MUST precede `except Exception`: in 3.11 asyncio.TimeoutError is the builtin
        # TimeoutError, a subclass of OSError and therefore of Exception.
        return {"ok": False, "error": "Connection timed out", "error_key": "connect_timeout"}
    except Exception as e:
        return {"ok": False, "error": f"Connection failed: {e}", "error_key": "connect_failed", "detail": str(e)}
    # The outer test_server wraps this whole coroutine in asyncio.wait_for(timeout).
    # Its inner wait_for below starts later, so the OUTER deadline fires first on a
    # slow list_tools -- and CancelledError is a BaseException, which neither
    # `except asyncio.TimeoutError` nor `except Exception` below can catch. Without
    # this try/finally, that path (surfaced to the caller as test_server's own
    # "Probe timed out" result) never reaches conn.aclose(), leaking the Client
    # context + AsyncExitStack (httpx2 client / unix socket / stdio bridge).
    try:
        try:
            metas, ttl = await asyncio.wait_for(conn.list_tools(), timeout=list_to)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "Listing tools timed out", "error_key": "list_timeout"}
        except Exception as e:
            return {"ok": False, "error": f"Listing tools failed: {e}", "error_key": "list_failed",
                    "detail": str(e)}
        proto = _protocol_fields(conn)
        instructions = _read_instructions(conn)
        server_info = _read_server_info(conn)
    finally:
        await conn.aclose()
    metas_out = [{"name": m["name"],
                  "schema_hash": schema_hash(m.get("input_schema")),
                  "desc_hash": desc_hash(m.get("description"))} for m in metas]
    schemas_out = [{"name": m["name"],
                    "description": m.get("description", "") or "",
                    "input_schema": m.get("input_schema")} for m in metas]
    return {"ok": True, "tool_count": len(metas), "tools": [m["name"] for m in metas],
            "instructions": instructions, "server_info": server_info,
            "ttl_sec": ttl, "tool_metas": metas_out, "schemas": schemas_out, **proto}
