# NimoOS-AI/agent/mcp_server/server.py
"""MCP Streamable-HTTP server: a thin SDK wiring over mcp_server.tools.
Auth + per-user context happen in the ASGI wrapper, before the SDK touches
the request, so an invalid token never allocates session/stream resources."""
from __future__ import annotations

import time

import mcp.types as mtypes
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

import mcp_tokens
from mcp_server import tools


def _build_lowlevel() -> Server:
    server = Server("nimoos-mcp")

    @server.list_tools()
    async def _list() -> list[mtypes.Tool]:
        return [mtypes.Tool(name=d["name"], description=d["description"],
                            inputSchema=d["inputSchema"])
                for d in tools.list_tool_defs()]

    @server.call_tool()
    async def _call(name: str, arguments: dict) -> list[mtypes.TextContent]:
        text = await tools.call(name, arguments or {})
        return [mtypes.TextContent(type="text", text=text)]

    return server


def build(conn):
    server = _build_lowlevel()
    session_manager = StreamableHTTPSessionManager(
        app=server, json_response=True, stateless=True)

    async def asgi(scope, receive, send):
        if scope["type"] != "http":
            return await session_manager.handle_request(scope, receive, send)
        headers = {k.decode().lower(): v.decode()
                   for k, v in (scope.get("headers") or [])}
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        uid = mcp_tokens.verify(conn, token, now_ms=int(time.time() * 1000))
        if not uid:
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body",
                        "body": b'{"error":"invalid or missing MCP token"}'})
            return
        # Same task as handle_request → ContextVars reach the tool dispatch.
        tools.setup_user_context(uid)
        await session_manager.handle_request(scope, receive, send)

    return asgi, session_manager
