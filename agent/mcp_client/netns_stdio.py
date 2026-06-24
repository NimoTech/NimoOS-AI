"""
agent/mcp_client/netns_stdio.py

MCPServerNetnsStdio — a subclass of _MCPServerWithClientSession that connects
to an MCP stdio server whose stdin/stdout have been bridged to a Unix socket by
the netns executor daemon (running inside the sandboxed network namespace).

Instead of spawning the subprocess directly (as MCPServerStdio does), this
class connects to a Unix socket and frames newline-delimited JSON-RPC messages
on top of it — exactly mirroring the framing in mcp/client/stdio/__init__.py
lines 139-189, but replacing process stdio with a socket byte-stream.

Framing contract (identical to stdio_client):
  read (socket → MemoryObjectReceiveStream[SessionMessage | Exception]):
      Buffer bytes; split on '\\n'; parse each line as JSONRPCMessage;
      wrap in SessionMessage; send to read_stream_writer.
  write (MemoryObjectSendStream[SessionMessage] → socket):
      Receive SessionMessage; serialize with model_dump_json(by_alias=True,
      exclude_none=True); append '\\n'; send bytes to socket.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import anyio
import anyio.lowlevel
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

import mcp.types as types
from mcp.shared.message import SessionMessage
from agents.mcp.server import _MCPServerWithClientSession

logger = logging.getLogger(__name__)


class MCPServerNetnsStdio(_MCPServerWithClientSession):
    """MCP server that connects via a Unix socket bridged to a stdio process
    running inside the netns executor's sandboxed network namespace."""

    def __init__(
        self,
        socket_path: str,
        name: str = "netns-stdio",
        cache_tools_list: bool = False,
        client_session_timeout_seconds: float | None = None,
        **kwargs,
    ):
        """
        Parameters
        ----------
        socket_path:
            Path to the Unix socket that bridges the MCP server's stdin/stdout.
            Created by the netns executor's mcp_stdio handler.
        name:
            Human-readable server name (used by the agents SDK for logging/errors).
        cache_tools_list:
            Passed to _MCPServerWithClientSession.
        client_session_timeout_seconds:
            Per-request read timeout for the ClientSession.
        **kwargs:
            Forwarded to _MCPServerWithClientSession for future-compatibility
            (tool_filter, use_structured_content, max_retry_attempts, etc.).
        """
        super().__init__(
            cache_tools_list=cache_tools_list,
            client_session_timeout_seconds=client_session_timeout_seconds,
            **kwargs,
        )
        self._socket_path = socket_path
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @asynccontextmanager
    async def create_streams(self):
        """Connect to the Unix socket and yield (read_stream, write_stream).

        The read_stream delivers SessionMessage | Exception objects from the
        server; the write_stream accepts SessionMessage objects to send.

        Framing mirrors mcp/client/stdio/__init__.py lines 117-189:
          - Buffer incoming bytes, split on newlines, parse JSON-RPC.
          - Serialize outgoing SessionMessages as compact JSON + newline.
        """
        read_stream_writer: MemoryObjectSendStream[SessionMessage | Exception]
        read_stream: MemoryObjectReceiveStream[SessionMessage | Exception]
        write_stream: MemoryObjectSendStream[SessionMessage]
        write_stream_reader: MemoryObjectReceiveStream[SessionMessage]

        read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
        write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

        sock_stream = await anyio.connect_unix(self._socket_path)

        async def _reader():
            """Byte-stream → SessionMessage.  Mirrors stdout_reader in stdio_client."""
            try:
                async with read_stream_writer:
                    buffer = b""
                    while True:
                        try:
                            chunk = await sock_stream.receive(4096)
                        except (anyio.EndOfStream, anyio.ClosedResourceError):
                            break
                        buffer += chunk
                        # Process all complete lines
                        while b"\n" in buffer:
                            line_bytes, buffer = buffer.split(b"\n", 1)
                            line = line_bytes.decode("utf-8", errors="replace").strip()
                            if not line:
                                continue
                            try:
                                message = types.JSONRPCMessage.model_validate_json(line)
                            except Exception as exc:
                                logger.exception(
                                    "MCPServerNetnsStdio: failed to parse JSONRPC message"
                                )
                                await read_stream_writer.send(exc)
                                continue
                            await read_stream_writer.send(SessionMessage(message))
            except anyio.ClosedResourceError:
                await anyio.lowlevel.checkpoint()

        async def _writer():
            """SessionMessage → byte-stream.  Mirrors stdin_writer in stdio_client."""
            try:
                async with write_stream_reader:
                    async for session_message in write_stream_reader:
                        json_str = session_message.message.model_dump_json(
                            by_alias=True, exclude_none=True
                        )
                        line = (json_str + "\n").encode("utf-8")
                        try:
                            await sock_stream.send(line)
                        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                            break
            except anyio.ClosedResourceError:
                await anyio.lowlevel.checkpoint()

        async with anyio.create_task_group() as tg:
            tg.start_soon(_reader)
            tg.start_soon(_writer)
            try:
                yield read_stream, write_stream
            finally:
                # Close write_stream so _writer task drains and exits
                await write_stream.aclose()
                # Close the socket to signal EOF to _reader
                await sock_stream.aclose()
