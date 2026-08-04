"""
agent/mcp_client/netns_stdio.py

netns_stdio_transport — an MCP `Transport` (see mcp.client.Transport)
that connects to an MCP stdio server whose stdin/stdout have been bridged to a
Unix socket by the netns executor daemon (running inside the sandboxed network
namespace).

This is a peer of the SDK's own `streamable_http_client` / `sse_client`: a third
transport, not a framework subclass. Instead of spawning the subprocess directly
(as the SDK's stdio transport does), it connects to a Unix socket and frames
newline-delimited JSON-RPC messages on top of it.

Framing contract (identical to the SDK's stdio transport):
  read (socket → MemoryObjectReceiveStream[SessionMessage | Exception]):
      Buffer bytes; split on '\\n'; parse each line as JSONRPCMessage;
      wrap in SessionMessage; send to read_stream_writer.
  write (MemoryObjectSendStream[SessionMessage] → socket):
      Receive SessionMessage; serialize with model_dump_json(by_alias=True,
      exclude_none=True); append '\\n'; send bytes to socket.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import anyio
import anyio.lowlevel
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

import mcp.types as types
from mcp.shared.message import SessionMessage

logger = logging.getLogger(__name__)


@asynccontextmanager
async def netns_stdio_transport(socket_path: str):
    """Connect to the Unix socket and yield (read_stream, write_stream).

    The read_stream delivers SessionMessage | Exception objects from the
    server; the write_stream accepts SessionMessage objects to send.
    """
    read_stream_writer: MemoryObjectSendStream[SessionMessage | Exception]
    read_stream: MemoryObjectReceiveStream[SessionMessage | Exception]
    write_stream: MemoryObjectSendStream[SessionMessage]
    write_stream_reader: MemoryObjectReceiveStream[SessionMessage]

    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    sock_stream = await anyio.connect_unix(socket_path)

    async def _reader():
        """Byte-stream → SessionMessage."""
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
                            message = types.jsonrpc_message_adapter.validate_json(line, by_name=False)
                        except Exception as exc:
                            logger.exception(
                                "netns_stdio_transport: failed to parse JSONRPC message"
                            )
                            await read_stream_writer.send(exc)
                            continue
                        await read_stream_writer.send(SessionMessage(message))
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()

    async def _writer():
        """SessionMessage → byte-stream."""
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
