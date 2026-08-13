"""Fetch the current user's decrypted MCP config from nimoos-ai's loopback
internal endpoint, using the one-time ticket the Go Proxy injected."""
from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

AI_URL_PATH = "/var/run/nimoos/ai.url"
RUNTIME_PATH = "/v1/ai/_internal/mcp/runtime"
FETCH_TIMEOUT = 3.0


@dataclass
class ConfigUnavailable:
    """The MCP runtime config could not be fetched — distinct from "no servers
    configured" ([]). agent.py renders this as a config-unavailable status
    instead of silently running without MCP tools (defect-1 silent point 2)."""
    reason: str


def parse_servers(payload: str) -> list[dict] | None:
    """Parse the runtime endpoint body into a list of server dicts, or None
    when the body is malformed — malformed is a failure, not "no servers"."""
    try:
        data = json.loads(payload)
    except Exception:
        return None
    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, list):
        return None
    return [s for s in servers if isinstance(s, dict)]


def _read_ai_base() -> str | None:
    try:
        with open(AI_URL_PATH, "r") as f:
            return f.read().strip() or None
    except Exception:
        return None


async def fetch_mcp_servers(ticket: str) -> list[dict] | ConfigUnavailable:
    """Returns the enabled MCP servers (decrypted) for the run: a list on
    success ([] really means "no servers configured"), or ConfigUnavailable
    when the config could not be fetched. Never raises — MCP is additive."""
    if not ticket:
        return ConfigUnavailable("no MCP ticket on this request")
    base = _read_ai_base()
    if not base:
        return ConfigUnavailable("ai.url is unreadable")
    url = base.rstrip("/") + RUNTIME_PATH
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
            resp = await client.get(url, headers={"X-Agent-MCP-Ticket": ticket})
    except Exception as e:
        return ConfigUnavailable(f"runtime config fetch failed: {e}")
    if resp.status_code != 200:
        return ConfigUnavailable(f"runtime config fetch returned HTTP {resp.status_code}")
    servers = parse_servers(resp.text)
    if servers is None:
        return ConfigUnavailable("runtime config response was malformed")
    return servers
