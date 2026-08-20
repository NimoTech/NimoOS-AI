"""Shared fixtures for test_mcp_description_propagation.py (design doc
Section 1.2.1's cross-layer chain — see that test module's own docstring for
the full chain diagram and for which link each fixture below exercises for
real versus stands in for).

Two things are deliberately real, unmodified production code:

  * ``FakeMcpServer`` is a real in-process MCP Streamable-HTTP server, built
    from the exact ``mcp.server.lowlevel.Server`` +
    ``StreamableHTTPSessionManager`` skeleton ``agent/mcp_server/server.py``
    uses for NimoOS's own tools (minus that file's bearer-token wrapper,
    which authenticates callers against NimoOS's own user DB and has no
    equivalent for a generic third-party server). Probing it speaks the real
    wire protocol end to end.

  * ``probe()``, ``approve()`` and the ``agent_run`` fixture all call the
    real ``mcp_client.client``/``mcp_client.runtime``/``skills.tool_gating``
    functions production code uses — ``test_server``, ``fetch_runtime``,
    ``fetch_schemas``, ``put_approval``, ``expand_categories``,
    ``_wrap_tool`` — never a mock standing in for any of them.

What is NOT the real Go service is ``FakeGoBackend`` plus the small HTTP
stub in front of it: a genuine SQLite-backed persistence layer that
replicates the exact invariants this test defends —
``service/mcp_runtime.go``'s ``SaveSuccess`` (every successful, non-empty
probe overwrites ``tools_json``/``schemas_json`` and advances ``listed_at``)
and ``service/mcp_approvals.go``'s ``EffectiveApprovals`` interface gate
(an approval is void once its recorded ``schema_hash`` no longer matches the
tool's current one; ``desc_hash`` is recorded but never gates anything).
Running the literal Go binary would additionally require reproducing its
JWT/Gateway bootstrap (see the task report for why that was judged
impractical and out of proportion here) — nothing in this repository's own
Go test suite does that either; every existing Go test in route/v2/*_test.go
binds handler methods directly to a bare echo instance for exactly the same
reason.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import socket
import sqlite3
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import mcp.types as mtypes
import pytest
import uvicorn
from agents import Agent
from confirm import ConfirmManager
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Mount

import mcp_client.client as mc
from mcp_client import runtime as mcp_runtime
from mcp_client.runtime import RuntimePayload
from skills import mcp_gating
from skills import tool_gating as tg

_server_ids = itertools.count(1)
_SERVERS: dict[int, "FakeMcpServer"] = {}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# FakeGoBackend: real SQLite, real listed_at/schema_hash invariants.
# ---------------------------------------------------------------------------


class FakeGoBackend:
    """Stand-in for the slice of Go's persistence this test exercises.

    Both tables and both methods below are a deliberate re-implementation of
    real Go SQL, not a canned-value mock:

      * ``save_success`` mirrors ``mcp_server_runtime``/``mcp_server_schemas``'s
        UPSERT in service/mcp_runtime.go's SaveSuccess — ``listed_at`` is
        unconditionally overwritten to ``now`` on every call, exactly the
        ``listed_at=excluded.listed_at`` clause Task 4's Go unit test pins.
      * ``effective_approvals`` mirrors service/mcp_approvals.go's
        EffectiveApprovals interface gate: an approval is only still
        effective if its stored schema_hash still matches the tool's
        CURRENT schema_hash. desc_hash is stored (for parity with the real
        row shape) but never compared — the same "participates in no gate"
        design the real Go code documents.
    """

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.executescript(
            """
            CREATE TABLE runtime (
                server_id INTEGER PRIMARY KEY,
                listed_at INTEGER NOT NULL DEFAULT 0,
                tools_json TEXT NOT NULL DEFAULT '[]',
                schemas_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE approvals (
                server_id INTEGER NOT NULL,
                tool_name TEXT NOT NULL,
                schema_hash TEXT NOT NULL DEFAULT '',
                desc_hash TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (server_id, tool_name)
            );
            """
        )
        self._conn.commit()

    def save_success(self, server_id: int, tool_metas: list[dict], schemas: list[dict]) -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO runtime (server_id, listed_at, tools_json, schemas_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(server_id) DO UPDATE SET
                    listed_at=excluded.listed_at,
                    tools_json=excluded.tools_json,
                    schemas_json=excluded.schemas_json
                """,
                (server_id, now, json.dumps(tool_metas), json.dumps(schemas)),
            )
            self._conn.commit()

    def runtime_row(self, server_id: int) -> tuple[int, list[dict], list[dict]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT listed_at, tools_json, schemas_json FROM runtime WHERE server_id=?",
                (server_id,),
            ).fetchone()
        if row is None:
            return 0, [], []
        listed_at, tools_json, schemas_json = row
        return listed_at, json.loads(tools_json), json.loads(schemas_json)

    def put_approval(self, server_id: int, tool_name: str, schema_hash: str, desc_hash: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO approvals (server_id, tool_name, schema_hash, desc_hash)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(server_id, tool_name) DO UPDATE SET
                    schema_hash=excluded.schema_hash, desc_hash=excluded.desc_hash
                """,
                (server_id, tool_name, schema_hash, desc_hash),
            )
            self._conn.commit()

    def effective_approvals(self, server_id: int) -> list[str]:
        _, tools, _ = self.runtime_row(server_id)
        current_schema_hash = {t["name"]: t.get("schema_hash", "") for t in tools}
        with self._lock:
            rows = self._conn.execute(
                "SELECT tool_name, schema_hash FROM approvals WHERE server_id=?",
                (server_id,),
            ).fetchall()
        return [
            tool_name
            for tool_name, schema_hash in rows
            if schema_hash and current_schema_hash.get(tool_name) == schema_hash
        ]


# ---------------------------------------------------------------------------
# HTTP stub in front of FakeGoBackend: the same four loopback endpoints
# mcp_client.runtime's fetch_runtime/fetch_schemas/put_approval/release_token
# talk to in production (route/v2/mcp.go's Runtime/SchemasInternal/
# ApprovalsInternal/ReleaseTokenInternal). Auth here is deliberately
# simplified to "ticket/token present and matching" — minting and expiring
# tickets/tokens is Go plumbing orthogonal to the listed_at/schema_hash chain
# this test defends; see the task report.
# ---------------------------------------------------------------------------

_SCHEMAS_PATH_RE = re.compile(r"^/v1/ai/_internal/mcp/servers/(\d+)/schemas$")


class _GoStubHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: N802 - silence access log
        pass

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _no_content(self) -> None:
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        httpd = self.server
        if self.path == "/v1/ai/_internal/mcp/runtime":
            if not self.headers.get("X-Agent-MCP-Ticket"):
                return self._json(401, {"message": "invalid mcp ticket"})
            listed_at, tools, _ = httpd.backend.runtime_row(httpd.server_id)
            approvals = [
                {"server_id": httpd.server_id, "tool_name": t}
                for t in httpd.backend.effective_approvals(httpd.server_id)
            ]
            httpd.write_token = f"tok-{time.time_ns()}"
            return self._json(
                200,
                {
                    "servers": [
                        {
                            "id": httpd.server_id,
                            "name": httpd.server_name,
                            "handle": httpd.server_name,
                            "transport": "http",
                            "url": httpd.mcp_url,
                            "command": "",
                            "args": [],
                            "env": {},
                            "headers": {},
                            "listed_at": listed_at,
                            "tools": tools,
                            "ttl_sec": 600,
                        }
                    ],
                    "approvals": approvals,
                    "write_token": httpd.write_token,
                },
            )
        m = _SCHEMAS_PATH_RE.match(self.path)
        if m:
            if self.headers.get("X-Agent-MCP-Write-Token") != httpd.write_token:
                return self._json(401, {"message": "invalid or missing mcp write token"})
            sid = int(m.group(1))
            listed_at, _, schemas = httpd.backend.runtime_row(sid)
            return self._json(200, {"listed_at": listed_at, "schemas": schemas})
        self._json(404, {"message": "not found"})

    def do_POST(self):  # noqa: N802
        httpd = self.server
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        if self.path == "/v1/ai/_internal/mcp/approvals":
            if self.headers.get("X-Agent-MCP-Write-Token") != httpd.write_token:
                return self._json(401, {"message": "invalid or missing mcp write token"})
            body = json.loads(raw or b"{}")
            sid = int(body.get("server_id") or 0)
            tool_name = str(body.get("tool_name") or "")
            _, tools, _ = httpd.backend.runtime_row(sid)
            meta = next((t for t in tools if t.get("name") == tool_name), None)
            schema_hash = meta.get("schema_hash", "") if meta else ""
            desc_hash = meta.get("desc_hash", "") if meta else ""
            httpd.backend.put_approval(sid, tool_name, schema_hash, desc_hash)
            return self._no_content()
        if self.path == "/v1/ai/_internal/mcp/token/release":
            return self._no_content()
        self._json(404, {"message": "not found"})


# ---------------------------------------------------------------------------
# FakeMcpServer: a real MCP protocol server with a live-mutable tool table,
# plus the FakeGoBackend + HTTP stub standing in for Go.
# ---------------------------------------------------------------------------


class FakeMcpServer:
    def __init__(self, server_id: int, name: str = "mail"):
        self.id = server_id
        self.name = name
        self.tools: dict[str, dict] = {}
        self.backend = FakeGoBackend()
        self.url = ""
        self.go_base_url = ""
        self._uv_server: uvicorn.Server | None = None
        self._uv_thread: threading.Thread | None = None
        self._go_httpd: ThreadingHTTPServer | None = None
        self._go_thread: threading.Thread | None = None

    def set_tool(self, name: str, *, description: str, schema: dict) -> None:
        self.tools[name] = {"description": description, "schema": schema}

    # --- lifecycle -----------------------------------------------------

    def start(self) -> None:
        self._start_mcp_server()
        self._start_go_stub()

    def stop(self) -> None:
        if self._uv_server is not None:
            self._uv_server.should_exit = True
            if self._uv_thread is not None:
                self._uv_thread.join(timeout=5)
        if self._go_httpd is not None:
            self._go_httpd.shutdown()
            if self._go_thread is not None:
                self._go_thread.join(timeout=5)

    def _start_mcp_server(self) -> None:
        async def _list(ctx, params) -> mtypes.ListToolsResult:
            return mtypes.ListToolsResult(
                tools=[
                    mtypes.Tool(name=name, description=t["description"], inputSchema=t["schema"])
                    for name, t in self.tools.items()
                ]
            )

        async def _call(ctx, params) -> mtypes.CallToolResult:
            if params.name not in self.tools:
                return mtypes.CallToolResult(
                    content=[mtypes.TextContent(type="text", text=f"unknown tool {params.name}")],
                    isError=True,
                )
            return mtypes.CallToolResult(content=[mtypes.TextContent(type="text", text="ok")])

        lowlevel = Server(f"fake-mcp-{self.name}", on_list_tools=_list, on_call_tool=_call)
        session_manager = StreamableHTTPSessionManager(app=lowlevel, json_response=True, stateless=True)

        @asynccontextmanager
        async def _lifespan(_app):
            async with session_manager.run():
                yield

        app = Starlette(
            lifespan=_lifespan,
            routes=[Mount("/", app=session_manager.handle_request)],
        )

        port = _free_port()
        config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10.0
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        if not server.started:
            raise RuntimeError("fake MCP server did not start within 10s")
        self._uv_server = server
        self._uv_thread = thread
        self.url = f"http://127.0.0.1:{port}/"

    def _start_go_stub(self) -> None:
        backend = self.backend
        server_id = self.id
        server_name = self.name
        mcp_url = self.url

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _GoStubHandler)
        httpd.backend = backend
        httpd.server_id = server_id
        httpd.server_name = server_name
        httpd.mcp_url = mcp_url
        httpd.write_token = ""
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self._go_httpd = httpd
        self._go_thread = thread
        self.go_base_url = f"http://127.0.0.1:{httpd.server_address[1]}"


# ---------------------------------------------------------------------------
# probe() / approve() / tool_desc(): plain helpers (not fixtures), called
# directly from the test bodies with fixture-provided objects as arguments —
# matching the task brief's own pseudocode shape.
# ---------------------------------------------------------------------------


def probe(server: FakeMcpServer) -> None:
    """Run the REAL probe (mcp_client.client.test_server — the exact function
    Go's probeAndPersist calls over HTTP in production, see agent/main.py's
    POST /agent/mcp/test) against `server`'s real MCP endpoint, then persist
    the result via FakeGoBackend.save_success — the stand-in for Go's
    SaveSuccess (link 1 of the design doc's chain)."""

    async def _do():
        cfg = {"transport": "http", "url": server.url, "headers": {}}
        result = await mc.test_server(cfg)
        assert result.get("ok"), f"probe against the fake MCP server failed: {result}"
        server.backend.save_success(server.id, result["tool_metas"], result["schemas"])

    asyncio.run(_do())


def approve(server_id: int, tool_name: str) -> None:
    """Seed a "don't ask again" approval for (server_id, tool_name), as if a
    user had approved it in an earlier session. Ensures the tool exists and
    has been probed at least once first (an approval always refers to a tool
    the user has actually seen), then records the approval through the REAL
    mcp_client.runtime.put_approval — the same production function
    _ensure_confirmed uses when a live user clicks "don't ask again" — so the
    write path is exercised for real too, not just the read path.
    """
    server = _SERVERS[server_id]
    if tool_name not in server.tools:
        server.set_tool(tool_name, description="(seed)", schema={"type": "object"})
        probe(server)
    elif server.backend.runtime_row(server_id)[0] == 0:
        probe(server)

    async def _do():
        payload = await mcp_runtime.fetch_runtime("seed-ticket")
        assert isinstance(payload, RuntimePayload), f"fetch_runtime failed while seeding approval: {payload}"
        ok = await mcp_runtime.put_approval(payload.write_token, server_id, tool_name)
        assert ok, "put_approval failed against the fake Go stub"

    asyncio.run(_do())


def tool_desc(result: "RunResult", fq_name: str) -> str:
    tool = next((t for t in result.tools if getattr(t, "name", "") == fq_name), None)
    assert tool is not None, (
        f"tool {fq_name!r} not found; have {[getattr(t, 'name', '') for t in result.tools]}"
    )
    return tool.description


# ---------------------------------------------------------------------------
# pytest fixtures
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    tools: list = field(default_factory=list)
    confirm_cards: list = field(default_factory=list)


@pytest.fixture
def fake_mcp_server(monkeypatch):
    # Module-global LRU (mcp_client.client._SCHEMA_CACHE) — must start empty
    # so an earlier test's cached schema body can never leak into this one.
    mc._SCHEMA_CACHE.clear()

    server_id = next(_server_ids)
    server = FakeMcpServer(server_id)
    server.start()
    _SERVERS[server_id] = server

    # Redirect the well-known service-discovery file mcp_client.runtime reads
    # in production (/var/run/nimoos/ai.url) to our fake Go stub. This is the
    # one substitution in the whole fixture chain that touches
    # mcp_client.runtime itself, and it substitutes environment discovery
    # only — every function in that module still runs unmodified, doing a
    # real HTTP round trip to whatever base URL it finds here.
    fd, path = tempfile.mkstemp(prefix="fake-ai-url-")
    with os.fdopen(fd, "w") as f:
        f.write(server.go_base_url)
    monkeypatch.setattr(mcp_runtime, "AI_URL_PATH", path)

    try:
        yield server
    finally:
        _SERVERS.pop(server_id, None)
        server.stop()
        os.unlink(path)


@pytest.fixture
def agent_run(fake_mcp_server, monkeypatch):
    # expand_categories persists unlocked categories via
    # skills.tool_gating._persist -> db.set_unlocked_categories, keyed off a
    # live `sessions` DB row this harness never creates. That persistence is
    # orthogonal to the MCP description-propagation chain under test here;
    # stubbed out exactly like tests/test_expand_tools.py already does for
    # the same reason.
    monkeypatch.setattr(tg, "_persist", lambda categories: None)

    def _run(unlock=(), call: str | None = None) -> RunResult:
        return _do_agent_run(fake_mcp_server, list(unlock), call)

    return _run


def _do_agent_run(server: FakeMcpServer, unlock: list[str], call: str | None) -> RunResult:
    # --- Phase 1 (async): the real run-start fetch, exactly as main.py's
    # /run endpoint does via mcp_client.runtime.fetch_runtime.
    payload = asyncio.run(mcp_runtime.fetch_runtime("run-ticket"))
    assert isinstance(payload, RuntimePayload), f"fetch_runtime failed: {payload}"

    agent_obj = Agent(name="test-agent", tools=[])
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE pending_confirmations (
            confirm_id TEXT PRIMARY KEY, session_id TEXT, action TEXT,
            description TEXT, command TEXT, created_at INTEGER)
        """
    )
    confirm_mgr = ConfirmManager(conn)
    events: list[dict] = []

    class _Sink:
        async def put(self, event):
            events.append(event)

    # Same ContextVar wiring agent.py's AgentRunner.run does at the start of
    # every real run (agent.py lines ~655-705) — reproduced here rather than
    # imported, since AgentRunner.run also drives an actual LLM turn, which
    # is out of scope for what this test defends (the MCP schema/description
    # plumbing, not model behavior).
    mc.SESSION_ID_VAR.set("test-session")
    mc.EVENT_QUEUE_VAR.set(_Sink())
    mc.CONFIRM_MGR_VAR.set(confirm_mgr)
    mc.USER_PATTERNS_VAR.set([])
    mc._RUN_CONNS_VAR.set({})
    mc._RUN_CONN_LOCKS_VAR.set({})
    mc.RUN_AGENT_VAR.set(agent_obj)
    mc._CONFIRMED_TOOLS_VAR.set(set(payload.approvals))
    mc._RUN_SERVERS_VAR.set({s["id"]: s for s in payload.servers if isinstance(s, dict) and "id" in s})
    mc.WRITE_TOKEN_VAR.set(payload.write_token)

    slugs = mc.assign_slugs(payload.servers)
    mcp_gating.MCP_HANDLES_VAR.set({slug: sid for sid, slug in slugs.items()})

    tg.GATING_SESSION_VAR.set("test-session")
    tg.UNLOCKED_VAR.set(set())

    # --- Phase 2 (sync, no running loop): the real function the model's
    # expand_tools call reaches. expand_categories's own docstring requires
    # this thread to have no running event loop (it calls asyncio.run()
    # internally for the L2 fetch) — exactly the constraint expand_tools
    # itself satisfies by handing this to a worker thread. Calling it from
    # inside an async function here would silently skip the L2 fetch instead
    # of raising (see _load_l2_tools's defensive branch), so this must stay
    # a plain synchronous call at this point in the sequence.
    tg.expand_categories(unlock)

    confirm_cards: list[dict] = []
    if call:
        tool = next((t for t in agent_obj.tools if getattr(t, "name", "") == call), None)
        assert tool is not None, (
            f"tool {call!r} not present after unlocking {unlock!r}; "
            f"have {[getattr(t, 'name', '') for t in agent_obj.tools]}"
        )

        async def _invoke():
            try:
                await asyncio.wait_for(tool.on_invoke_tool(None, "{}"), timeout=5.0)
            except asyncio.TimeoutError:
                pass  # expected: a pending confirmation card is never answered by this harness
            finally:
                # A tool call that was already approved actually connects to
                # the fake MCP server for real (see _get_run_conn). Close it
                # here, inside the SAME event loop it was opened in — closing
                # it later (e.g. implicitly via GC after asyncio.run()
                # returns and the loop is gone) throws a cross-task cancel
                # scope error from anyio's teardown.
                await mc.close_run_conns()

        asyncio.run(_invoke())
        confirm_cards = [e for e in events if e.get("type") == "confirmation_required"]

    return RunResult(tools=list(agent_obj.tools), confirm_cards=confirm_cards)
