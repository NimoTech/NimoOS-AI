"""Pin the exact mcp 2.0 SDK symbols this project imports.

Rationale: the upgrade plan was written against a design doc whose facts came
from a separate venv. Every import path the client-side refactor depends on is
asserted here ONCE, so a wrong assumption fails in this file rather than
surfacing as a confusing error deep inside _connect. If the SDK moves a symbol,
this test tells you exactly which one — fix the import here and everywhere it is
used, do not work around it.
"""
import inspect

import pytest


def test_client_class_and_kwargs():
    from mcp.client import Client
    params = inspect.signature(Client.__init__).parameters
    for kw in ("mode", "read_timeout_seconds", "input_required_max_rounds", "cache"):
        assert kw in params, f"Client.__init__ lost keyword {kw!r}"


def test_transport_protocol_exported():
    from mcp.client import Transport
    assert Transport is not None


def test_transport_factories():
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamable_http_client
    assert "http_client" in inspect.signature(streamable_http_client).parameters
    assert callable(sse_client)


def test_error_types():
    from mcp.client import InputRequiredRoundsExceededError
    from mcp.shared.exceptions import MCPError
    assert issubclass(MCPError, Exception)
    assert issubclass(InputRequiredRoundsExceededError, Exception)


def test_invalid_request_code():
    from mcp.types import INVALID_REQUEST
    assert isinstance(INVALID_REQUEST, int)


def test_list_tools_result_ttl_field():
    from mcp.types import ListToolsResult
    r = ListToolsResult(tools=[])
    assert r.ttl_ms == 0                     # new-protocol default
    assert hasattr(r, "cache_scope")


def test_memory_stream_helper():
    from mcp.shared.memory import create_client_server_memory_streams
    assert callable(create_client_server_memory_streams)


def test_netns_framing_symbols_still_exist():
    import mcp.types as types
    from mcp.shared.message import SessionMessage
    # NOT `hasattr(types, "JSONRPCMessage")` -- that's trivially true in mcp 2.0
    # (it's a types.UnionType alias, not something production code calls) and
    # would pass even if the real dependency below broke. netns_stdio.py:72
    # actually calls `types.jsonrpc_message_adapter.validate_json(...)` to parse
    # framed JSON-RPC lines off the wire -- pin THAT symbol instead.
    assert hasattr(types, "jsonrpc_message_adapter")
    assert hasattr(types.jsonrpc_message_adapter, "validate_json")
    assert SessionMessage is not None


def test_httpx2_importable():
    import httpx2
    assert hasattr(httpx2, "AsyncClient")


def test_server_side_symbols_unchanged():
    from mcp.server.lowlevel import Server
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    params = inspect.signature(StreamableHTTPSessionManager.__init__).parameters
    assert "json_response" in params and "stateless" in params
    assert Server is not None
