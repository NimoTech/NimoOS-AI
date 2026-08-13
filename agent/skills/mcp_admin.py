"""Agent tool: register an external MCP server for the current user, with an
in-chat confirmation. The server's tools become available on the user's NEXT
message (tools are built at run start). Backend write goes through nimoos-ai's
localhost-only _internal endpoints (the agent holds no user JWT)."""
from __future__ import annotations

import httpx
from agents import function_tool

import mcp_client.client as mc
import skills.skills_registry as skills_registry
from mcp_client.runtime import _read_ai_base

PARSE_PATH = "/v1/ai/_internal/mcp/parse"
REGISTER_PATH = "/v1/ai/_internal/mcp/register"
HTTP_TIMEOUT = 10.0


class ParseError(Exception):
    pass


async def _parse(base: str, command_line: str) -> dict:
    """Call the internal parse endpoint. Raises ParseError on 4xx/failure."""
    url = base.rstrip("/") + PARSE_PATH
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(url, json={"command_line": command_line})
    except Exception as e:
        raise ParseError(str(e))
    if resp.status_code >= 400:
        raise ParseError(resp.text[:200])
    return resp.json()


async def _register(base: str, user_id: str, command_line: str, name: str) -> dict:
    """Call the internal register endpoint. Returns the created DTO. Raises on failure."""
    url = base.rstrip("/") + REGISTER_PATH
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(url, json={
            "user_id": user_id, "command_line": command_line, "name": name})
    if resp.status_code >= 400:
        raise RuntimeError(resp.text[:200])
    return resp.json()


@function_tool
async def add_mcp_server(command_line: str, name: str = "") -> str:
    """Register an external MCP server for the user from a one-line command.

    Use when the user asks to install/add/connect an MCP server and gives a
    command like `npx -y @upstash/context7-mcp`, `uvx mcp-server-time`, a
    `codex mcp add ... -- ...` line, or a bare https URL. The user is shown a
    confirmation card with the exact command before anything is registered.
    On approval the server is saved; its tools become available on the user's
    NEXT message (not this turn). `name` is optional (derived from the package
    if omitted).
    """
    base = _read_ai_base()
    if not base:
        return ("System error: cannot locate the nimoos-ai service, so the MCP server "
                "cannot be registered. Tell the user to add it manually on the settings page.")
    try:
        parsed = await _parse(base, command_line)
    except ParseError as e:
        return f"Parse failed: {e}. Check the command line (e.g. `npx -y @scope/pkg`)."

    display_name = (name or "").strip() or parsed.get("suggested_name") or "mcp"
    transport = parsed.get("transport", "stdio")

    mgr = mc.CONFIRM_MGR_VAR.get()
    queue = mc.EVENT_QUEUE_VAR.get()
    session_id = mc.SESSION_ID_VAR.get()
    if mgr is None or queue is None:
        return ("System error: the confirmation channel is unavailable; cannot register. "
                "Tell the user to add it manually on the settings page.")

    confirm_id = mgr.register(
        session_id, f"mcp_install:{display_name}",
        f'Register MCP server "{display_name}" ({transport})',
        command_line)
    await queue.put({
        "type": "confirmation_required", "confirm_id": confirm_id,
        "kind": "mcp_install", "name": display_name, "transport": transport,
        "command": parsed.get("command", ""), "args": parsed.get("args", []),
        "url": parsed.get("url", ""),
    })
    confirmed = await mgr.wait(confirm_id)
    if not confirmed:
        return "The user declined to install this MCP server."

    user_id = skills_registry.USER_ID_VAR.get()
    try:
        await _register(base, user_id, command_line, display_name)
    except Exception as e:
        return f"Registration failed: {e}. The user can add it manually in Settings (AI → MCP servers)."
    return (f'Registered MCP server "{display_name}" ({transport}). '
            f"Its tools will be available from your next message.")


ALL_TOOLS = [add_mcp_server]
