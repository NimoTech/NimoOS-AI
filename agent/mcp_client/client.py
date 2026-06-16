"""Wrap MCP tools as confirm-gated, blacklist-gated FunctionTools."""
from __future__ import annotations

import json
import re
from contextvars import ContextVar

from agents import FunctionTool

from mcp_client.schema import sanitize_schema, flatten_result

# Set per-run by agent.py (mirrors how skills/* receive context).
SESSION_ID_VAR: ContextVar = ContextVar("mcp_session_id", default="")
EVENT_QUEUE_VAR: ContextVar = ContextVar("mcp_event_queue", default=None)
CONFIRM_MGR_VAR: ContextVar = ContextVar("mcp_confirm_mgr", default=None)
USER_PATTERNS_VAR: ContextVar = ContextVar("mcp_user_patterns", default=[])
# session-scoped set of "serverid::toolname" the user chose to remember.
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
