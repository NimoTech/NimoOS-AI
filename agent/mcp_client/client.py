"""Wrap MCP tools as confirm-gated, blacklist-gated FunctionTools."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from contextvars import ContextVar

from agents import FunctionTool

from mcp_client.schema import sanitize_schema, flatten_result

MCP_CONNECT_TIMEOUT = 5  # seconds; hard cap on the run-start CONNECT path (keeps startup non-blocking)

# ClientSession read timeout — bounds each JSON-RPC request (list_tools AND call_tool),
# NOT just the connect. Must be generous: remote tool calls (e.g. MS Learn semantic
# search) routinely take several seconds, far past the 5s connect cap. call_tool has no
# outer wait_for, so it is bounded ONLY by this value — too small here silently cancels
# every slow tool call mid-flight (surfaces as httpx.ConnectTimeout/CancelledError).
MCP_SESSION_TIMEOUT = 60  # seconds

STDIO_CONNECT_TIMEOUT = 90  # seconds; the first stdio npx/uvx package fetch can be slow (cached locally after, then fast)

# ── stdio command allow-list (2026-07-16 hardening) ───────────────────────────
# A registered stdio MCP server spawns command+args directly in the netns
# executor, bypassing the shell guard. Without this, a user tricked into
# approving `mcp_register_server("bash -c 'rm -rf /DATA'")` would run an
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


SCHEMA_TTL = 600        # seconds; past this it goes stale (still usable, triggers a background revalidate)
SCHEMA_CACHE_MAX = 256  # LRU capacity cap, bounds memory use


class _CacheEntry:
    __slots__ = ("metas", "fetched_at", "fingerprint")

    def __init__(self, metas, fetched_at, fingerprint):
        self.metas = metas              # list[dict]: {"name","description","input_schema"}
        self.fetched_at = fetched_at    # time.monotonic()
        self.fingerprint = fingerprint


_SCHEMA_CACHE: "OrderedDict[int, _CacheEntry]" = OrderedDict()  # key = server["id"], LRU
_REVALIDATING: set = set()             # re-entrancy guard: only one background refresh per server at a time
_BACKGROUND_TASKS: set = set()         # strong refs so asyncio.create_task tasks aren’t GC’d


def _extract_meta(mcp_tool) -> dict:
    return {"name": mcp_tool.name,
            "description": getattr(mcp_tool, "description", "") or "",
            "input_schema": getattr(mcp_tool, "inputSchema", None)}


def _cache_put(server_id: int, metas, fingerprint) -> None:
    _SCHEMA_CACHE[server_id] = _CacheEntry(metas, time.monotonic(), fingerprint)
    _SCHEMA_CACHE.move_to_end(server_id)
    while len(_SCHEMA_CACHE) > SCHEMA_CACHE_MAX:
        _SCHEMA_CACHE.popitem(last=False)   # evict least-recently-used


def _cache_get(server_id: int):
    entry = _SCHEMA_CACHE.get(server_id)
    if entry is not None:
        _SCHEMA_CACHE.move_to_end(server_id)
    return entry


def _fingerprint(server: dict) -> str:
    basis = json.dumps({
        "transport": server.get("transport", "http"),
        "url": server.get("url", ""),
        "headers": server.get("headers", {}),
        "command": server.get("command", ""),
        "args": server.get("args", []),
        "env": server.get("env", {}),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(basis.encode()).hexdigest()


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


def _wrap_tool(server: dict, meta: dict) -> FunctionTool:
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
    """Holds a live agents.mcp server. `srv` exposes connect/list_tools/
    call_tool/cleanup (the agents.mcp server interface)."""
    __slots__ = ("server", "srv")

    def __init__(self, server: dict, srv):
        self.server = server
        self.srv = srv

    async def call_tool(self, name: str, args: dict):
        return await self.srv.call_tool(name, args)

    async def aclose(self):
        try:
            await self.srv.cleanup()
        except Exception:
            pass


async def _emit_warning(server_name: str, err) -> None:
    queue = EVENT_QUEUE_VAR.get()
    if queue is None:
        return
    await queue.put({"type": "mcp_warning", "server": server_name, "error": str(err)})


async def _connect(server: dict, connect_timeout: int = None) -> "McpConn":
    transport = server.get("transport", "http")
    # connect_timeout caps the TCP+TLS+initialize handshake. The run-start cold path
    # uses the short default (_connect_timeout) to stay non-blocking; call-time /
    # background / test callers pass a generous value because a COLD connect to a
    # remote server can take several seconds (measured ~5.6s to learn.microsoft.com)
    # — well past the 5s run-start cap. session timeout bounds each request after
    # connect (list/call); see MCP_SESSION_TIMEOUT.
    connect_to = connect_timeout if connect_timeout is not None else _connect_timeout(server)
    session_to = _session_timeout(server)
    if transport in ("http", "sse"):
        from agents.mcp import MCPServerStreamableHttp, MCPServerSse
        cls = MCPServerStreamableHttp if transport == "http" else MCPServerSse
        srv = cls(
            params={"url": server["url"], "headers": server.get("headers", {})},
            client_session_timeout_seconds=session_to,
            name=server.get("name", "mcp"),
        )
    elif transport == "stdio":
        # Deny-by-default gate: the stdio command spawns directly in the netns
        # executor, bypassing the shell guard — never spawn an off-list command.
        _assert_stdio_command_allowed(server.get("command", ""))
        from mcp_client.netns_stdio import MCPServerNetnsStdio
        import netns.client as netns_client
        socket_path = await netns_client.start_mcp_stdio(
            command=server["command"],
            args=server.get("args", []),
            env=_stdio_env(server.get("env", {})),
            connect_timeout=connect_to,
        )
        srv = MCPServerNetnsStdio(
            socket_path=socket_path,
            name=server.get("name", "mcp"),
            cache_tools_list=False,
            client_session_timeout_seconds=session_to,
        )
    else:
        raise ValueError(f"unsupported transport: {transport}")
    await asyncio.wait_for(srv.connect(), timeout=connect_to)
    return McpConn(server=server, srv=srv)


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
        # connect (generous cap) instead of failing the call at the 5s run-start cap.
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
        # both connect and list (tolerates a cold remote connect that the 5s
        # run-start cap would reject). This is what self-heals a slow server.
        conn = await _connect(server, connect_timeout=_session_timeout(server))
        try:
            tools = await asyncio.wait_for(conn.srv.list_tools(), timeout=_session_timeout(server))
            _cache_put(server["id"], [_extract_meta(t) for t in tools], _fingerprint(server))
        finally:
            await conn.aclose()
    except Exception:
        pass   # keep stale cache; background task must never raise


async def _cold_fetch(server: dict):
    """Connect once just to read schemas; cache + return metas. Connection is
    closed immediately (real calls use the per-run lazy connection)."""
    conn = await _connect(server)
    try:
        tools = await asyncio.wait_for(conn.srv.list_tools(), timeout=_connect_timeout(server))
    finally:
        try:
            await conn.aclose()
        except Exception:
            pass
    metas = [_extract_meta(t) for t in tools]
    _cache_put(server["id"], metas, _fingerprint(server))
    return metas


async def _metas_for_server(server: dict):
    """Return tool metas for a server, preferring cache. Cold/changed -> fetch
    inline; stale -> serve cached + background revalidate. Returns [] on failure
    (and emits a warning)."""
    fp = _fingerprint(server)
    entry = _cache_get(server["id"])
    if entry is not None and entry.fingerprint == fp:
        if time.monotonic() - entry.fetched_at > SCHEMA_TTL:
            _schedule_revalidate(server)        # stale-while-revalidate
        return entry.metas
    # cold / fingerprint changed:
    if server.get("transport") == "stdio":
        _schedule_revalidate(server)            # background single-flight self-healing warmup (connect+list+cache), does not block run startup
        await _emit_warning(server.get("name", "mcp"),
                            "stdio tools are initializing in the background for first use; retry shortly")
        return []
    try:
        return await _cold_fetch(server)        # http/sse: fetch inline (short timeout, does not block run startup)
    except Exception as e:
        # A cold-fetch timeout is usually a cold connection on a slow link (>5s).
        # Warm the cache in the background with a generous timeout; tools are ready by the next run.
        _schedule_revalidate(server)
        await _emit_warning(server.get("name", "mcp"), e)
        return []


async def build_mcp_tools(servers: list[dict]) -> list:
    """Build confirm/blacklist-gated FunctionTools for this run from the schema
    cache (zero connection when warm). Connections are established lazily per
    tool call (see _get_run_conn). Returns a flat list of FunctionTools."""
    metas_per = await asyncio.gather(*[_metas_for_server(s) for s in servers],
                                     return_exceptions=True)
    tools = []
    seen_names: set = set()
    for s, metas in zip(servers, metas_per):
        if isinstance(metas, Exception):
            await _emit_warning(s.get("name", "mcp"), metas)
            continue
        for meta in metas:
            tool = _wrap_tool(s, meta)
            if tool.name in seen_names:          # disambiguate cross-server collisions
                suffix = 2
                while f"{tool.name}_{suffix}" in seen_names:
                    suffix += 1
                tool.name = f"{tool.name}_{suffix}"
            seen_names.add(tool.name)
            tools.append(tool)
    return tools


TEST_TIMEOUT = 9  # seconds; must be < the Go caller’s timeout (10s), so Python actively cancels and releases after Go gives up
STDIO_TEST_TIMEOUT = 90  # seconds; the first stdio package fetch is slow; the Go /test client timeout must be > this value


async def test_server(server: dict) -> dict:
    timeout = STDIO_TEST_TIMEOUT if server.get("transport") == "stdio" else TEST_TIMEOUT
    try:
        return await asyncio.wait_for(_test_server_inner(server), timeout=timeout)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Probe timed out", "error_key": "probe_timeout"}


async def _test_server_inner(server: dict) -> dict:
    # The probe's overall bound is the outer test_server wait_for(budget). Use that
    # same budget for the inner connect + list so a cold remote connect (~5.6s) isn't
    # rejected by the short 5s run-start cap; the outer wait_for is the real ceiling.
    budget = STDIO_TEST_TIMEOUT if server.get("transport") == "stdio" else TEST_TIMEOUT
    try:
        conn = await _connect(server, connect_timeout=budget)
    except Exception as e:
        return {"ok": False, "error": f"Connection failed: {e}", "error_key": "connect_failed", "detail": str(e)}
    try:
        tools = await asyncio.wait_for(conn.srv.list_tools(), timeout=budget)
    except asyncio.TimeoutError:
        await conn.aclose()
        return {"ok": False, "error": "Listing tools timed out", "error_key": "list_timeout"}
    except Exception as e:
        await conn.aclose()
        return {"ok": False, "error": f"Listing tools failed: {e}", "error_key": "list_failed", "detail": str(e)}
    await conn.aclose()
    metas = [_extract_meta(t) for t in tools]
    if "id" in server:
        _cache_put(server["id"], metas, _fingerprint(server))
    return {"ok": True, "tool_count": len(metas), "tools": [m["name"] for m in metas]}
