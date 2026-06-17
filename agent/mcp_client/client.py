"""Wrap MCP tools as confirm-gated, blacklist-gated FunctionTools."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict
from contextvars import ContextVar

from agents import FunctionTool

from mcp_client.schema import sanitize_schema, flatten_result

MCP_CONNECT_TIMEOUT = 5  # seconds; hard cap on the run-start path

SCHEMA_TTL = 600        # 秒;超过则 stale(仍可用,触发后台 revalidate)
SCHEMA_CACHE_MAX = 256  # LRU 容量上限,兜住内存


class _CacheEntry:
    __slots__ = ("metas", "fetched_at", "fingerprint")

    def __init__(self, metas, fetched_at, fingerprint):
        self.metas = metas              # list[dict]: {"name","description","input_schema"}
        self.fetched_at = fetched_at    # time.monotonic()
        self.fingerprint = fingerprint


_SCHEMA_CACHE: "OrderedDict[int, _CacheEntry]" = OrderedDict()  # key = server["id"], LRU
_REVALIDATING: set = set()             # 防重入:同一 server 同时只有一个后台刷新
_BACKGROUND_TASKS: set = set()         # 强引用,防 asyncio.create_task 被 GC


def _extract_meta(mcp_tool) -> dict:
    return {"name": mcp_tool.name,
            "description": getattr(mcp_tool, "description", "") or "",
            "input_schema": getattr(mcp_tool, "inputSchema", None)}


def _cache_put(server_id: int, metas, fingerprint) -> None:
    _SCHEMA_CACHE[server_id] = _CacheEntry(metas, time.monotonic(), fingerprint)
    _SCHEMA_CACHE.move_to_end(server_id)
    while len(_SCHEMA_CACHE) > SCHEMA_CACHE_MAX:
        _SCHEMA_CACHE.popitem(last=False)   # 淘汰最久未用


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
                raise ValueError(f"参数 {k} 命中黑名单({pat})")


async def _ensure_confirmed(server: dict, tool_name: str, args: dict) -> bool:
    key = f"{server['id']}::{tool_name}"
    if key in _CONFIRMED_TOOLS_VAR.get(set()):
        return True
    mgr = CONFIRM_MGR_VAR.get()
    queue = EVENT_QUEUE_VAR.get()
    session_id = SESSION_ID_VAR.get()
    confirm_id = mgr.register(
        session_id, f"mcp_call:{key}",
        f"调用 MCP server「{server['name']}」的工具 {tool_name}",
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
            return f"已被黑名单拦截: {e}"
        if not await _ensure_confirmed(server, tool_name, args):
            return "用户拒绝了该 MCP 工具调用"
        try:
            conn = await _get_run_conn(server)          # lazy connect (connection layer)
        except Exception as e:
            return (f"系统错误:无法连接到 MCP 服务方「{server['name']}」({e})。"
                    f"这是连接/服务端故障,与调用参数无关——请勿改参数重试,"
                    f"告知用户检查该 MCP 服务状态。")
        try:
            result = await conn.call_tool(tool_name, args)   # tool execution layer
        except Exception as e:
            return f"MCP 工具 {tool_name} 执行出错: {e}"
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


async def _connect(server: dict) -> "McpConn":
    transport = server.get("transport", "http")
    if transport in ("http", "sse"):
        from agents.mcp import MCPServerStreamableHttp, MCPServerSse
        cls = MCPServerStreamableHttp if transport == "http" else MCPServerSse
        srv = cls(
            params={"url": server["url"], "headers": server.get("headers", {})},
            client_session_timeout_seconds=MCP_CONNECT_TIMEOUT,
            name=server.get("name", "mcp"),
        )
    else:  # stdio reserved for phase 2
        raise ValueError(f"unsupported transport: {transport}")
    await asyncio.wait_for(srv.connect(), timeout=MCP_CONNECT_TIMEOUT)
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
        conn = await _connect(server)
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
        conn = await _connect(server)
        try:
            tools = await asyncio.wait_for(conn.srv.list_tools(), timeout=MCP_CONNECT_TIMEOUT)
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
        tools = await asyncio.wait_for(conn.srv.list_tools(), timeout=MCP_CONNECT_TIMEOUT)
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
    try:
        return await _cold_fetch(server)        # cold or fingerprint changed
    except Exception as e:
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
