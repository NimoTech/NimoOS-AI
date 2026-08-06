"""New-and-old protocol dual support, end to end, with no HTTP and no network.

This is the core promise of the mcp-2.0-upgrade: our client code (`_connect`/
`McpConn` in mcp_client/client.py) must be able to talk to BOTH a legacy
(pre-2026-07-28 initialize-handshake) server and a modern (2026-07-28
per-request-envelope) server. As of phase 2 it declares elicitation (both the
form and url sub-capabilities — see `test_we_declare_both_elicitation_modes_
but_still_no_sampling_or_roots` below) because `_connect` now passes an
`elicitation_callback`; sampling and roots stay undeclared, since we pass no
callbacks for them and the spec forbids a server from asking a client that
never declared a capability. That is documented behaviour of the SDK; this
file is what turns it into a lock rather than an assumption.

Scaffolding note (read before touching this file): the task-7 brief drafted
this test against decorator-style `@server.list_tools()` registration and a
hand-rolled `_static_transport`/`_serving` pair driving `Server.run` directly
over `create_client_server_memory_streams()`. Neither survived contact with
the real SDK:

  * `@server.list_tools()` does not exist on mcp 2.0's `Server` — Task 1B
    already migrated production (`mcp_server/server.py::_build_lowlevel`) to
    the constructor-injection form (`Server(name, on_list_tools=..., ...)`,
    handlers shaped `(ctx, params) -> Result`). This file follows the same
    shape so the test server matches how our own production server is built.

  * `create_client_server_memory_streams` + manually driving `Server.run()`
    in a task group (the brief's `_serving`) is real and works, but the SDK
    ships a purpose-built wrapper for exactly this — `mcp.client._memory.
    InMemoryTransport(server, raise_exceptions=...)` — that does the same
    thing with EOF-based (not cancel-based) teardown of the server task.
    `InMemoryTransport` is not re-exported from `mcp.client`'s public
    `__init__.py`, but it IS imported and used internally by
    `mcp.client.client` itself (`_connect_inproc`'s legacy arm), so it is not
    a private implementation detail invented for this test — it is the
    SDK's own answer to "run a Server in-process for a Client to talk to".
    Passing an *already-constructed* `InMemoryTransport` instance to
    `Client(...)` (rather than the bare `Server`) is deliberate: `Client`
    special-cases a bare `Server`/`MCPServer` argument (`_connect_inproc`) to
    skip streams and JSON-RPC framing entirely for non-legacy modes (a
    `DirectDispatcher` peer pair) — great for the SDK's own unit tests, but
    NOT representative of our production transports (`streamable_http_client`
    / `sse_client` / the netns stdio transport), which are all real
    `Transport`s (stream pairs, real JSON-RPC framing). Wrapping the `Server`
    in `InMemoryTransport` ourselves and handing `Client` *that* keeps both
    parametrized modes on the same stream+JSON-RPC-framing code path our real
    transports use — `Client.__post_init__` sees "not a Server/MCPServer/str"
    and dispatches through `_connect_transport`, exactly like the real thing.

  * The brief's capability-capture scaffolding tried to swap out
    `server.request_handlers[mtypes.ListToolsRequest]` — that attribute is
    gone (2.0 dispatch is a private `_request_handlers` dict keyed by method
    *string*, e.g. "tools/list", not by request-type class), and reading
    `req.params.meta.clientCapabilities` assumed the request handler receives
    a request object at all — it doesn't; it receives `(ctx, params)`. Digging
    through mcp/server/{connection,session,context}.py: the correct, mode-
    agnostic observation point the SDK itself uses internally (see
    server/apps.py::_client_capabilities) is `ctx.session.client_capabilities`
    — a `types.ClientCapabilities | None` that both eras keep in lockstep:
    the legacy `initialize` handshake commits it via `Connection.client_params`'s
    setter (server/connection.py:253-259), and the modern per-request envelope
    sets it directly via `Connection.from_envelope` (server/connection.py:264-296).
    No monkeypatching of server internals needed — we just read it inside our
    own `on_list_tools` handler, the same handler production already installs.

None of this loosens the three assertions the brief pins down as
non-negotiable: both modes list AND call; an un-hinted server's ttl resolves
to `SCHEMA_TTL`; and `clientCapabilities` on the wire has both elicitation
sub-capabilities (`form`, `url`) but no sampling/roots key. See
mcp/client/session.py::_build_capabilities + adopt()/send_discover() for why
reading `ClientCapabilities.model_dump(..., exclude_none=True)` (rather than
eyeballing which attributes are `None`) is the faithful "on the wire" check:
that exact call, with that exact flag, is literally what the SDK serializes
into `params._meta.clientCapabilities` for every modern request.
"""
from contextlib import AsyncExitStack

import mcp.types as mtypes
import pytest
from mcp.client import Client
from mcp.client._memory import InMemoryTransport
from mcp.client.session import HANDSHAKE_PROTOCOL_VERSIONS
from mcp.server.lowlevel import Server

import mcp_client.client as mc
from mcp_client.schema import flatten_result


def _build_server(capture: dict | None = None) -> Server:
    """A minimal dual-era MCP server, built the same (constructor-injection)
    way production builds one — see mcp_server/server.py::_build_lowlevel.
    `Server.run()` (driven for us by `InMemoryTransport`) serves BOTH the
    legacy initialize handshake and the modern per-request envelope; which
    one a given connection speaks is decided by that connection's first
    request, not by anything set here.

    When `capture` is given, `on_list_tools` records the requesting client's
    declared capabilities (see module docstring for why `ctx.session.
    client_capabilities` is the right — and only correct — place to read
    this from a `(ctx, params)`-shaped handler).
    """
    async def _list(ctx, params) -> mtypes.ListToolsResult:
        if capture is not None:
            capture["caps"] = ctx.session.client_capabilities
        return mtypes.ListToolsResult(tools=[
            mtypes.Tool(name="echo", description="echo back",
                        inputSchema={"type": "object",
                                     "properties": {"text": {"type": "string"}}})])

    async def _call(ctx, params) -> mtypes.CallToolResult:
        text = (params.arguments or {}).get("text", "")
        return mtypes.CallToolResult(content=[mtypes.TextContent(type="text", text=f"echo:{text}")])

    return Server("compat-test", on_list_tools=_list, on_call_tool=_call)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_list_and_call_over_both_protocol_paths(mode):
    """Both "auto" (modern per-request envelope, negotiated via server/discover)
    and "legacy" (the old initialize handshake) must be able to list AND call
    a tool through OUR OWN McpConn — not just bare SDK Client calls — since
    McpConn.list_tools()/call_tool() is the thin layer production actually
    goes through (mcp_client/client.py)."""
    server = _build_server()
    transport = InMemoryTransport(server, raise_exceptions=True)
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(
            Client(transport, mode=mode,
                   read_timeout_seconds=5,
                   input_required_max_rounds=mc.MCP_INPUT_REQUIRED_ROUNDS,
                   cache=None))

        # Prove `mode` actually took the path it claims, instead of silently
        # negotiating the other one: exactly one of discover_result/
        # initialize_result is populated per mcp/client/session.py::adopt(),
        # and it must be the one this mode names.
        if mode == "legacy":
            assert client.session.initialize_result is not None
            assert client.session.discover_result is None
            assert client.session.protocol_version in HANDSHAKE_PROTOCOL_VERSIONS
        else:
            assert client.session.discover_result is not None
            assert client.session.initialize_result is None

        conn = mc.McpConn(server={"id": 1, "name": "t"}, client=client, stack=stack)

        metas, ttl = await conn.list_tools()
        assert [m["name"] for m in metas] == ["echo"]
        assert ttl == mc.SCHEMA_TTL          # server sets no ttlMs -> our default

        result = await conn.call_tool("echo", {"text": "hi"})
        assert "echo:hi" in flatten_result(result)


@pytest.mark.asyncio
async def test_we_declare_both_elicitation_modes_but_still_no_sampling_or_roots():
    """Phase 2 reverses exactly one third of the phase-1 assertion.

    Both sub-capabilities must be pinned, not just "elicitation is non-empty": the
    whole justification for shipping the form card AND the url card together is that
    mcp/client/session.py::_build_capabilities constructs
    `ElicitationCapability(form=FormElicitationCapability(),
                           url=UrlElicitationCapability())`
    unconditionally the moment the callback differs from the SDK default — there is no
    form-only setting. If a future SDK makes them separately selectable, this test must
    go RED first, rather than letting the URL card quietly become dead code that never
    receives a request. (Empirically verified today: the server observes
    `{'elicitation': {'form': {}, 'url': {}}}`.)

    sampling / roots stay undeclared: sampling would let a third-party server spend our
    model budget and inject prompts into our model, and not declaring is the strongest
    defence there is — a compliant server then never asks.
    """
    capture: dict = {}
    server = _build_server(capture)
    transport = InMemoryTransport(server, raise_exceptions=True)

    async def _cb(context, params):
        return mtypes.ElicitResult(action="decline")

    async with Client(transport, mode="auto",
                      read_timeout_seconds=5,
                      input_required_max_rounds=mc.MCP_INPUT_REQUIRED_ROUNDS,
                      cache=None,
                      elicitation_callback=_cb) as client:
        await client.list_tools()

    caps = capture.get("caps")
    assert caps is not None, "server never observed a clientCapabilities envelope"
    dumped = caps.model_dump(exclude_none=True)
    assert dumped.get("elicitation") == {"form": {}, "url": {}}
    assert caps.elicitation.form is not None
    assert caps.elicitation.url is not None
    assert "sampling" not in dumped
    assert "roots" not in dumped
