"""Wrap MCP tools as confirm-gated, blacklist-gated FunctionTools."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict
from contextvars import ContextVar

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

from agents import FunctionTool

from mcp_client.schema import sanitize_schema, flatten_result

# Set per-run by agent.py (mirrors how skills/* receive context).
SESSION_ID_VAR: ContextVar = ContextVar("mcp_session_id", default="")
EVENT_QUEUE_VAR: ContextVar = ContextVar("mcp_event_queue", default=None)
CONFIRM_MGR_VAR: ContextVar = ContextVar("mcp_confirm_mgr", default=None)
USER_PATTERNS_VAR: ContextVar = ContextVar("mcp_user_patterns", default=[])
# session-scoped set of "serverid::toolname" the user chose to remember.
# CONTRACT: agent.py MUST call _CONFIRMED_TOOLS_VAR.set(set()) at the start of
# each run (see Task 13) to prevent cross-run/session bleed of approvals.
_CONFIRMED_TOOLS_VAR: ContextVar = ContextVar("mcp_confirmed_tools", default=set())

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


async def _ensure_confirmed(server: dict, mcp_tool, args: dict) -> bool:
    key = f"{server['id']}::{mcp_tool.name}"
    if key in _CONFIRMED_TOOLS_VAR.get(set()):
        return True
    mgr = CONFIRM_MGR_VAR.get()
    queue = EVENT_QUEUE_VAR.get()
    session_id = SESSION_ID_VAR.get()
    confirm_id = mgr.register(
        session_id, f"mcp_call:{key}",
        f"调用 MCP server「{server['name']}」的工具 {mcp_tool.name}",
        json.dumps(args, ensure_ascii=False)[:500])
    await queue.put({
        "type": "confirmation_required", "confirm_id": confirm_id,
        "kind": "mcp_tool", "server": server["name"], "tool": mcp_tool.name,
        "remember_scope": "tool",
    })
    confirmed = await mgr.wait(confirm_id)
    if confirmed and mgr.consume_remember(confirm_id):
        s = _CONFIRMED_TOOLS_VAR.get(set())
        s.add(key)
        _CONFIRMED_TOOLS_VAR.set(s)
    return confirmed


def _wrap_tool(server: dict, conn, mcp_tool) -> FunctionTool:
    slug = _slug(server["name"])
    fq_name = f"mcp__{slug}__{mcp_tool.name}"
    schema = sanitize_schema(getattr(mcp_tool, "inputSchema", None))

    async def _on_invoke(ctx, input_json: str) -> str:
        try:
            args = json.loads(input_json or "{}")
        except Exception:
            args = {}
        try:
            _gate_args(args, USER_PATTERNS_VAR.get([]))
        except ValueError as e:
            return f"已被黑名单拦截: {e}"
        if not await _ensure_confirmed(server, mcp_tool, args):
            return "用户拒绝了该 MCP 工具调用"
        try:
            result = await conn.call_tool(mcp_tool.name, args)
        except Exception as e:
            return f"MCP 工具 {mcp_tool.name} 调用失败: {e}"
        return flatten_result(result)

    return FunctionTool(
        name=fq_name,
        description=getattr(mcp_tool, "description", "") or "",
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


async def build_mcp_tools(servers: list[dict]):
    """Connect all servers concurrently; a failing server is skipped (its tools
    are simply absent this run). Returns (tools, conns_to_close)."""
    results = await asyncio.gather(*[_connect(s) for s in servers],
                                   return_exceptions=True)
    tools, conns = [], []
    seen_names: set[str] = set()
    for s, conn in zip(servers, results):
        if isinstance(conn, Exception):
            await _emit_warning(s.get("name", "mcp"), conn)
            continue
        try:
            mcp_tools = await asyncio.wait_for(conn.srv.list_tools(),
                                               timeout=MCP_CONNECT_TIMEOUT)
        except Exception as e:
            await _emit_warning(s.get("name", "mcp"), e)
            await conn.aclose()
            continue
        conns.append(conn)
        for t in mcp_tools:
            tool = _wrap_tool(s, conn, t)
            # Disambiguate name collisions across servers (e.g. two servers whose
            # slug+toolname coincide) so neither silently shadows the other.
            if tool.name in seen_names:
                suffix = 2
                while f"{tool.name}_{suffix}" in seen_names:
                    suffix += 1
                tool.name = f"{tool.name}_{suffix}"
            seen_names.add(tool.name)
            tools.append(tool)
    return tools, conns
