"""Fetch the current user's decrypted MCP config from nimoos-ai's loopback
internal endpoint, using the one-time ticket the Go Proxy injected."""
from __future__ import annotations

import json

import httpx

AI_URL_PATH = "/var/run/nimoos/ai.url"
RUNTIME_PATH = "/v1/ai/_internal/mcp/runtime"
FETCH_TIMEOUT = 3.0


def parse_servers(payload: str) -> list[dict]:
    """Parse the runtime endpoint body into a list of server dicts. Returns []
    on any malformed input — MCP is additive and must never break a run."""
    try:
        data = json.loads(payload)
    except Exception:
        return []
    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, list):
        return []
    return [s for s in servers if isinstance(s, dict)]


def _read_ai_base() -> str | None:
    try:
        with open(AI_URL_PATH, "r") as f:
            return f.read().strip() or None
    except Exception:
        return None


async def fetch_mcp_servers(ticket: str) -> list[dict]:
    """Returns enabled MCP servers (decrypted) for the run, or [] when the ticket
    is missing, ai.url is unreadable, or the fetch fails."""
    if not ticket:
        return []
    base = _read_ai_base()
    if not base:
        return []
    url = base.rstrip("/") + RUNTIME_PATH
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
            resp = await client.get(url, headers={"X-Agent-MCP-Ticket": ticket})
        if resp.status_code != 200:
            return []
        return parse_servers(resp.text)
    except Exception:
        return []
