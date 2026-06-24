"""
tests/test_mcp_netns.py

Task 7 — MCPServerNetnsStdio framing tests + resource-cleanup regression tests.

Approach: framing unit-tests (not a full MCP handshake).

Why framing unit-tests instead of a full MCP handshake:
  A full handshake requires a compliant MCP server binary at test time, which
  is not guaranteed in CI / test environments and would pull in real subprocess
  spawning + network-namespace concerns.  The critical new code in Task 7 is
  create_streams() — the framing layer that bridges a Unix socket to anyio
  MemoryObjectStreams.  Framing tests directly exercise:
    (a) writer path: send a SessionMessage → socket receives correct <json>\\n bytes
    (b) reader path: write <json>\\n to socket → read_stream delivers a SessionMessage
  This approach was chosen: framing unit-tests (option b from the brief).

Resource-cleanup tests (I-1, I-2, Minor-1) added in Task 7 review fix:
  test_mcp_stdio_sock_unlinked_after_bridge: sock file removed after bridge ends
  test_mcp_stdio_proc_reaped_after_bridge:  proc is terminated/reaped after bridge
  test_mcp_stdio_env_npx_and_proxy_enforced: npx/uvx env vars present AND proxy forced

Tests do NOT require root or a real netns.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

import anyio
import anyio.lowlevel
import pytest
import pytest_asyncio

import mcp.types as types
from mcp.shared.message import SessionMessage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_jsonrpc_ping() -> dict:
    return {"jsonrpc": "2.0", "method": "ping", "id": 1}


def _make_jsonrpc_result() -> dict:
    return {"jsonrpc": "2.0", "result": {}, "id": 1}


def _make_session_message(d: dict) -> SessionMessage:
    msg = types.JSONRPCMessage.model_validate(d)
    return SessionMessage(msg)


# ---------------------------------------------------------------------------
# Fixture: a minimal echo unix-socket server
# The server reads one line, echoes it back, then closes the connection.
# ---------------------------------------------------------------------------

async def _echo_server(socket_path: str, ready: asyncio.Event, stop: asyncio.Event):
    """Listen on *socket_path*, accept one conn, echo one line, then stop."""
    server = await anyio.create_unix_listener(socket_path)
    ready.set()
    async with server:
        # Accept exactly one connection
        async with anyio.create_task_group() as tg:
            async def _handle():
                conn_stream = await server.accept()
                async with conn_stream:
                    # Read one newline-terminated line
                    buf = b""
                    while b"\n" not in buf:
                        chunk = await conn_stream.receive(4096)
                        buf += chunk
                    line = buf.split(b"\n")[0]
                    # Echo it back
                    await conn_stream.send(line + b"\n")
                stop.set()
                tg.cancel_scope.cancel()

            tg.start_soon(_handle)


# ---------------------------------------------------------------------------
# Step 1 (TDD): confirm test_netns_create_streams_writer_framing FAILS before
# netns_stdio.py exists.  These are the real tests.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_netns_create_streams_writer_framing(tmp_path):
    """
    (a) Writer path: sending a SessionMessage through write_stream results in
    the correct <json>\\n bytes arriving on the unix socket.
    """
    from mcp_client.netns_stdio import MCPServerNetnsStdio

    sock_path = str(tmp_path / "test.sock")
    ready = asyncio.Event()
    stop = asyncio.Event()

    # Start echo server in background
    task = asyncio.create_task(
        _echo_server(sock_path, ready, stop)
    )
    await asyncio.wait_for(ready.wait(), timeout=2.0)

    try:
        srv = MCPServerNetnsStdio(
            socket_path=sock_path,
            name="test-mcp",
            cache_tools_list=False,
            client_session_timeout_seconds=None,
        )

        # Open just the streams (not full connect/session) via the context manager
        async with srv.create_streams() as (read_stream, write_stream):
            # Send a message via the write_stream
            msg_dict = _make_jsonrpc_ping()
            sm = _make_session_message(msg_dict)
            await write_stream.send(sm)

            # The echo server sends it back — we should receive it on read_stream
            received = await asyncio.wait_for(read_stream.receive(), timeout=2.0)
            assert isinstance(received, SessionMessage)
            dumped = received.message.model_dump(by_alias=True, exclude_none=True)
            assert dumped["jsonrpc"] == "2.0"
            assert dumped["id"] == 1

    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_netns_create_streams_reader_framing(tmp_path):
    """
    (b) Reader path: writing raw <json>\\n bytes to the socket results in a
    correctly parsed SessionMessage arriving on read_stream.
    """
    from mcp_client.netns_stdio import MCPServerNetnsStdio

    sock_path = str(tmp_path / "reader_test.sock")

    # Start a server that sends one message then waits
    ready = asyncio.Event()
    stop = asyncio.Event()

    async def _send_one_server():
        server = await anyio.create_unix_listener(sock_path)
        ready.set()
        async with server:
            async with anyio.create_task_group() as tg:
                async def _handle():
                    conn_stream = await server.accept()
                    async with conn_stream:
                        # Send one JSONRPC message to the client
                        msg = _make_jsonrpc_result()
                        line = (json.dumps(msg) + "\n").encode()
                        await conn_stream.send(line)
                        # Wait for client to read it (give time)
                        await anyio.sleep(1.0)
                    stop.set()
                    tg.cancel_scope.cancel()

                tg.start_soon(_handle)

    server_task = asyncio.create_task(_send_one_server())
    await asyncio.wait_for(ready.wait(), timeout=2.0)

    try:
        srv = MCPServerNetnsStdio(
            socket_path=sock_path,
            name="test-mcp-reader",
            cache_tools_list=False,
            client_session_timeout_seconds=None,
        )

        async with srv.create_streams() as (read_stream, write_stream):
            received = await asyncio.wait_for(read_stream.receive(), timeout=2.0)
            assert isinstance(received, SessionMessage)
            dumped = received.message.model_dump(by_alias=True, exclude_none=True)
            assert dumped["jsonrpc"] == "2.0"
            assert dumped["result"] == {}
            assert dumped["id"] == 1

    finally:
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):
            pass


# ---------------------------------------------------------------------------
# executor.py: test that mcp_stdio kind is handled
# ---------------------------------------------------------------------------

def test_executor_handles_mcp_stdio_kind(tmp_path, monkeypatch):
    """executor._execute returns a socket path for kind='mcp_stdio'."""
    import netns.executor as executor_mod
    # Use a writable tmp dir for MCP sockets instead of /var/run/nimoos/
    monkeypatch.setattr(executor_mod, "MCP_SOCK_DIR", str(tmp_path))

    from netns.executor import _execute

    # Use /bin/cat which is a valid binary and accepts stdin/stdout.
    # It will stay alive reading stdin, so the bridge has time to bind.
    resp = _execute({
        "id": "test-1",
        "kind": "mcp_stdio",
        "command": "/bin/cat",
        "args": [],
        "env": {},
    })
    # Key assertion: no "unsupported kind" error
    assert "unsupported kind" not in resp.get("error", ""), (
        f"executor did not handle kind='mcp_stdio': {resp}"
    )
    # It should have a socket_path
    assert "id" in resp
    assert "socket_path" in resp, f"Expected socket_path in response, got: {resp}"
    assert resp["socket_path"].startswith(str(tmp_path))


# ---------------------------------------------------------------------------
# client.py: test that stdio branch now uses MCPServerNetnsStdio
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_stdio_uses_netns(monkeypatch):
    """
    _connect() for transport='stdio' must use MCPServerNetnsStdio
    (not MCPServerStdio) so the subprocess lives in the executor netns.
    """
    import mcp_client.client as mc

    captured = {}

    class FakeNetnsStdio:
        def __init__(self, socket_path=None, name=None,
                     cache_tools_list=False,
                     client_session_timeout_seconds=None, **kwargs):
            captured["socket_path"] = socket_path
            captured["name"] = name

        async def connect(self):
            captured["connected"] = True

    class FakeNetnsClient:
        @staticmethod
        async def start_mcp_stdio(command, args, env, **kwargs):
            captured["command"] = command
            captured["args"] = args
            return "/var/run/nimoos/agent-mcp-test.sock"

    import mcp_client.netns_stdio as ns_mod
    monkeypatch.setattr(ns_mod, "MCPServerNetnsStdio", FakeNetnsStdio)

    import netns.client as netns_client_mod
    monkeypatch.setattr(netns_client_mod, "start_mcp_stdio", FakeNetnsClient.start_mcp_stdio)

    server = {
        "id": 42,
        "name": "test-stdio-server",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "some-mcp"],
        "env": {"K": "V"},
    }
    conn = await mc._connect(server)
    assert captured.get("connected") is True, "MCPServerNetnsStdio.connect() not called"
    assert captured.get("command") == "npx"
    assert captured.get("args") == ["-y", "some-mcp"]
    assert captured.get("socket_path") == "/var/run/nimoos/agent-mcp-test.sock"
    assert captured.get("name") == "test-stdio-server"


# ---------------------------------------------------------------------------
# Task 7 review fix — I-1 / I-2 / Minor-1 resource-cleanup regression tests
# ---------------------------------------------------------------------------

def test_mcp_stdio_sock_unlinked_after_bridge(tmp_path, monkeypatch):
    """I-1: sock file must NOT exist after the bridge thread finishes.

    Approach: spawn /bin/cat as the MCP server, connect a client to the bridge
    socket, then close the connection and give the bridge thread time to tear
    down.  The sock file must have been unlinked.
    """
    import socket as sock_mod
    import time
    import netns.executor as executor_mod

    monkeypatch.setattr(executor_mod, "MCP_SOCK_DIR", str(tmp_path))

    # Spawn the bridge
    resp = executor_mod._execute({
        "id": "sock-cleanup-test",
        "kind": "mcp_stdio",
        "command": "/bin/cat",
        "args": [],
        "env": {},
    })
    assert "socket_path" in resp, f"Expected socket_path: {resp}"
    sock_path = resp["socket_path"]
    assert os.path.exists(sock_path), "Socket file should exist before connecting"

    # Connect as MCP client, then close immediately to trigger bridge teardown
    with sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM) as client:
        client.connect(sock_path)
        client.shutdown(sock_mod.SHUT_RDWR)

    # Bridge thread cleans up asynchronously — give it up to 3 seconds
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not os.path.exists(sock_path):
            break
        time.sleep(0.05)

    assert not os.path.exists(sock_path), (
        f"I-1 regression: sock file still exists after bridge ended: {sock_path}"
    )


def test_mcp_stdio_proc_reaped_after_bridge(tmp_path, monkeypatch):
    """I-2: MCP server process must be terminated/reaped after bridge ends.

    Spawn /bin/cat, connect and disconnect, then assert proc.poll() is not None
    (process is no longer alive).
    """
    import socket as sock_mod
    import subprocess
    import time
    import netns.executor as executor_mod

    monkeypatch.setattr(executor_mod, "MCP_SOCK_DIR", str(tmp_path))

    # We need access to the Popen object; monkeypatch Popen to capture it
    _original_popen = subprocess.Popen
    captured_proc = {}

    class _CapturePopen(_original_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured_proc["proc"] = self

    monkeypatch.setattr(subprocess, "Popen", _CapturePopen)

    resp = executor_mod._execute({
        "id": "proc-reap-test",
        "kind": "mcp_stdio",
        "command": "/bin/cat",
        "args": [],
        "env": {},
    })
    assert "socket_path" in resp, f"Expected socket_path: {resp}"
    sock_path = resp["socket_path"]

    proc = captured_proc.get("proc")
    assert proc is not None, "Failed to capture Popen object"
    assert proc.poll() is None, "Process should still be running before connecting"

    # Connect and disconnect to trigger bridge teardown
    with sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM) as client:
        client.connect(sock_path)
        client.shutdown(sock_mod.SHUT_RDWR)

    # Wait for process to be reaped (up to 5 seconds for terminate+wait)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.05)

    assert proc.poll() is not None, (
        f"I-2 regression: MCP server proc (pid={proc.pid}) still alive after bridge ended"
    )


def test_mcp_stdio_env_npx_and_proxy_enforced(tmp_path, monkeypatch):
    """Minor-1 + Task-5 invariant: npx/uvx env vars must reach the subprocess
    AND HTTP_PROXY must remain the enforced proxy despite caller passing a
    different value.

    Approach: spawn 'bash -c env' as the MCP server so it prints its env to
    stdout, connect to read that output, then parse and assert.
    """
    import socket as sock_mod
    import time
    import netns.executor as executor_mod
    from netns import bootstrap

    monkeypatch.setattr(executor_mod, "MCP_SOCK_DIR", str(tmp_path))

    # Inject npm/uv vars into the executor's os.environ so _build_proxy_env picks them up
    monkeypatch.setenv("npm_config_cache", "/tmp/test-npm-cache")
    monkeypatch.setenv("UV_CACHE_DIR", "/tmp/test-uv-cache")
    monkeypatch.setenv("NIMOOS_MCP_HOME", "/tmp/test-mcp-home")

    # The caller tries to override HTTP_PROXY (must be blocked)
    caller_env = {
        "HTTP_PROXY": "http://evil.example.com:1234",
        "npm_config_cache": "/tmp/caller-npm",  # should be preserved (caller wins over executor default)
    }

    resp = executor_mod._execute({
        "id": "env-test",
        "kind": "mcp_stdio",
        "command": "/bin/bash",
        "args": ["-c", "env; echo __ENV_DONE__"],
        "env": caller_env,
    })
    assert "socket_path" in resp, f"Expected socket_path: {resp}"
    sock_path = resp["socket_path"]

    # Collect env output from the bridge socket
    env_output = b""
    try:
        with sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM) as client:
            client.settimeout(5.0)
            client.connect(sock_path)
            # Read until we see __ENV_DONE__ or connection closes
            while True:
                try:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    env_output += chunk
                    if b"__ENV_DONE__" in env_output:
                        break
                except OSError:
                    break
    except OSError:
        pass

    env_text = env_output.decode("utf-8", errors="replace")

    # Parse key=value lines
    env_vars = {}
    for line in env_text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            env_vars[k.strip()] = v.strip()

    # Caller-supplied npm_config_cache must win over executor env default
    assert env_vars.get("npm_config_cache") == "/tmp/caller-npm", (
        f"npm_config_cache not in subprocess env or wrong value; got: {env_vars.get('npm_config_cache')}"
    )

    # UV_CACHE_DIR from executor os.environ must appear (caller didn't supply it)
    assert env_vars.get("UV_CACHE_DIR") == "/tmp/test-uv-cache", (
        f"UV_CACHE_DIR missing or wrong; got: {env_vars.get('UV_CACHE_DIR')}"
    )

    # Task-5 invariant: HTTP_PROXY must be the enforced proxy, not the evil one
    enforced_proxy = f"http://{bootstrap.PROXY_IP}:8888"
    assert env_vars.get("HTTP_PROXY") == enforced_proxy, (
        f"HTTP_PROXY not enforced; got: {env_vars.get('HTTP_PROXY')!r}, expected: {enforced_proxy!r}"
    )
