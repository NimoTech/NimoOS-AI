"""Fetch the current user's decrypted MCP config from nimoos-ai's loopback
internal endpoint, using the one-time ticket the Go Proxy injected."""
from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

AI_URL_PATH = "/var/run/nimoos/ai.url"
RUNTIME_PATH = "/v1/ai/_internal/mcp/runtime"
APPROVALS_PATH = "/v1/ai/_internal/mcp/approvals"
SCHEMAS_PATH_FMT = "/v1/ai/_internal/mcp/servers/{}/schemas"
TOKEN_RELEASE_PATH = "/v1/ai/_internal/mcp/token/release"
WRITE_TOKEN_HEADER = "X-Agent-MCP-Write-Token"
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


@dataclass
class RuntimePayload:
    """Everything the agent needs from Go's single Runtime response at run
    start, beyond the raw server list: the set of tool calls already approved
    ("don't ask again") for this user, and a run-scoped write token used to
    record new approvals, fetch full schemas, and release the token later."""
    servers: list[dict]
    approvals: set[str]
    write_token: str


def parse_runtime(payload: str) -> RuntimePayload | None:
    """Parse the Runtime endpoint body into servers + approvals + write_token.

    Two distinct failure shapes matter here, and they must not be conflated:

    - Malformed body (not JSON, not an object, or "servers" missing/not a
      list) is a hard failure: returns None, exactly like parse_servers.
    - A body that has a valid "servers" list but lacks "approvals" and/or
      "write_token" is NOT malformed — it is what an older Go build (one
      that predates this task's endpoints) sends today. MCP is an add-on
      capability, so this must still produce a usable RuntimePayload with
      empty defaults (approvals=set(), write_token=""), never None. A run
      must never fail to start just because approvals/write_token are absent.
    """
    servers = parse_servers(payload)
    if servers is None:
        return None

    try:
        data = json.loads(payload)
    except Exception:
        # Unreachable in practice — parse_servers above already parsed this
        # same payload as JSON successfully — kept only for defense in depth.
        return None

    approvals: set[str] = set()
    raw_approvals = data.get("approvals") if isinstance(data, dict) else None
    if isinstance(raw_approvals, list):
        for entry in raw_approvals:
            if not isinstance(entry, dict):
                continue
            server_id = entry.get("server_id")
            tool_name = entry.get("tool_name")
            if server_id is None or tool_name is None:
                continue
            approvals.add(f"{server_id}::{tool_name}")

    write_token = data.get("write_token") if isinstance(data, dict) else None
    if not isinstance(write_token, str):
        write_token = ""

    return RuntimePayload(servers=servers, approvals=approvals, write_token=write_token)


def _read_ai_base() -> str | None:
    try:
        with open(AI_URL_PATH, "r") as f:
            return f.read().strip() or None
    except Exception:
        return None


async def fetch_runtime(ticket: str) -> "RuntimePayload | ConfigUnavailable":
    """Fetch the Runtime endpoint and parse the FULL response (Task 8: the
    server list plus Go's pre-filtered per-user approval set and a
    run-scoped write token) via parse_runtime. This is the sole production
    entry point for run start (main.py's /run endpoint); agent.py's
    AgentRunner.run consumes the resulting RuntimePayload directly.

    A near-identical predecessor, fetch_mcp_servers (which parsed only the
    plain server list via parse_servers, discarding approvals/write_token),
    was deleted once this replaced its one production call site — two copies
    of the same fetch-and-degrade logic would only drift. parse_servers
    itself stays: parse_runtime still uses it internally, and it has its own
    direct tests (test_parse_servers_*).

    Never raises — MCP is additive."""
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
    payload = parse_runtime(resp.text)
    if payload is None:
        return ConfigUnavailable("runtime config response was malformed")
    return payload


def _parse_schemas_body(payload: str) -> tuple[int, list[dict]] | None:
    """Parse the schemas endpoint body into (listed_at, schemas), or None when
    the body is malformed. Kept separate from fetch_schemas so the parsing
    logic can be exercised without going through httpx."""
    try:
        data = json.loads(payload)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    listed_at = data.get("listed_at")
    if not isinstance(listed_at, int):
        return None
    schemas = data.get("schemas")
    if not isinstance(schemas, list):
        return None
    return listed_at, [s for s in schemas if isinstance(s, dict)]


async def put_approval(write_token: str, server_id: int, tool_name: str) -> bool:
    """Record one "don't ask again" approval for (server_id, tool_name).
    Returns whether the write was recorded.

    Degradation rule (design doc §5.4 — not negotiable): the caller MUST
    ignore a False return and let the call the user just approved proceed
    anyway. These two outcomes are not symmetric. Failing to persist "don't
    ask again" only costs convenience — the user gets asked again the next
    time this tool is called, which is annoying but harmless. Refusing to
    honor an approval the user has *just* explicitly granted, purely because
    the write-back to Go failed, is a broken product: it would silently
    override a decision the user already made in this very turn. So a write
    failure here may only ever degrade to "ask again later", never to
    "block a call already approved this turn".
    """
    base = _read_ai_base()
    if not base:
        return False
    url = base.rstrip("/") + APPROVALS_PATH
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
            resp = await client.post(
                url,
                json={"server_id": server_id, "tool_name": tool_name},
                headers={WRITE_TOKEN_HEADER: write_token},
            )
    except Exception:
        return False
    return resp.status_code == 204


async def fetch_schemas(write_token: str, server_id: int) -> tuple[int, list[dict]]:
    """Fetch server_id's full tool schema bodies (the L2, on-demand expansion)
    using the run-scoped write token. Note: the design plan text for this
    call predates the token requirement Go actually enforces — the endpoint
    401s without X-Agent-MCP-Write-Token, so this call sends it.

    Returns (listed_at, schemas) on success. Degrades to (0, []) on a network
    error, a non-200 status, or a malformed/non-JSON body: schema expansion
    is additive (it only lets the model see more tool detail), so a failure
    here must never raise into the agent's tool-calling loop. 0 reads the
    same way an unprobed server's listed_at reads everywhere else in this
    codebase — "nothing to show yet", not an error — which is also exactly
    what Task 13's listed_at-keyed cache needs to treat this as a miss.
    """
    base = _read_ai_base()
    if not base:
        return 0, []
    url = base.rstrip("/") + SCHEMAS_PATH_FMT.format(server_id)
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
            resp = await client.get(url, headers={WRITE_TOKEN_HEADER: write_token})
    except Exception:
        return 0, []
    if resp.status_code != 200:
        return 0, []
    parsed = _parse_schemas_body(resp.text)
    if parsed is None:
        return 0, []
    return parsed


async def release_token(write_token: str) -> None:
    """Release the run-scoped write token at run teardown, shrinking its
    replay window back down to this run's actual duration instead of leaving
    it valid for Go's 24h backstop (see RunTokenStore on the Go side).

    Best-effort cleanup only: the endpoint always returns 204 and treats a
    missing/unknown token as a harmless no-op, so there is nothing a caller
    could usefully react to. A network error is swallowed here rather than
    raised, matching that same always-succeeds, fire-and-forget contract —
    a failed release is not worse than never calling this at all.
    """
    base = _read_ai_base()
    if not base:
        return
    url = base.rstrip("/") + TOKEN_RELEASE_PATH
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
            await client.post(url, headers={WRITE_TOKEN_HEADER: write_token})
    except Exception:
        pass
