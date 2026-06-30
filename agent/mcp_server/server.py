# NimoOS-AI/agent/mcp_server/server.py
"""MCP Streamable-HTTP server: a thin SDK wiring over mcp_server.tools.
Auth + per-user context happen in the ASGI wrapper, before the SDK touches
the request, so an invalid token never allocates session/stream resources."""
from __future__ import annotations

import time

import anyio
import mcp.types as mtypes
from mcp.server.lowlevel import Server
from mcp.server.streamable_http import StreamableHTTPServerTransport
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


async def _handle_stateless_inline(server: Server, scope, receive, send,
                                   json_response: bool) -> None:
    """Run a single stateless MCP request inline (own task group per request).

    Used as a fallback when the shared session_manager has not been started
    (e.g. TestClient used without the context-manager form).  In production the
    startup handler pre-warms the shared task group via session_manager.run(),
    which is more efficient; this path is only hit when that hasn't happened."""
    transport = StreamableHTTPServerTransport(
        mcp_session_id=None,
        is_json_response_enabled=json_response,
    )

    async def _run_server(*, task_status=anyio.TASK_STATUS_IGNORED):
        async with transport.connect() as (read_stream, write_stream):
            task_status.started()
            try:
                await server.run(
                    read_stream,
                    write_stream,
                    server.create_initialization_options(),
                    stateless=True,
                )
            except Exception:
                pass

    async with anyio.create_task_group() as tg:
        await tg.start(_run_server)
        await transport.handle_request(scope, receive, send)
        await transport.terminate()


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
        if session_manager._task_group is None:
            # Startup handler hasn't run (e.g. TestClient without context-manager
            # form). Fall back to an inline task group for this request.
            await _handle_stateless_inline(server, scope, receive, send,
                                           json_response=True)
        else:
            await session_manager.handle_request(scope, receive, send)

    return asgi, session_manager
