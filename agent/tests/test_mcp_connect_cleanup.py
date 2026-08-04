"""Connect-path resource cleanup.

_connect enters the SDK Client into an AsyncExitStack. Every caller wraps it in
asyncio.wait_for, so the most common failure is a CancelledError arriving mid
handshake — the cleanup must therefore be shielded, or the httpx client / unix
socket is never released.
"""
import asyncio

import pytest

import mcp_client.client as mc


class _HangingClient:
    """Stands in for mcp.client.Client: an async CM that never finishes __aenter__."""

    async def __aenter__(self):
        await asyncio.sleep(3600)
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_connect_cancelled_midway_closes_stack(monkeypatch):
    closed = []

    async def fake_build_transport(server, stack, connect_to, session_to):
        # Stand in for whatever the real transport registers for teardown
        # (the httpx client on http/sse, the socket on stdio).
        stack.push_async_callback(_record_closed, closed)
        return object()

    monkeypatch.setattr(mc, "_build_transport", fake_build_transport)
    monkeypatch.setattr(mc, "Client", lambda *a, **k: _HangingClient())

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            mc._connect({"id": 1, "name": "x", "transport": "http", "url": "https://x"}),
            timeout=0.05,
        )
    # transport cleanup callback ran despite the cancellation
    assert closed, "connect cleanup was skipped when cancelled — resources leaked"


async def _record_closed(sink):
    sink.append(True)


@pytest.mark.asyncio
async def test_aclose_is_cancel_shielded(monkeypatch):
    """McpConn.aclose() runs inside _cold_fetch's finally, which is itself inside
    an outer wait_for. A bare await there would be re-cancelled immediately."""
    closed = []

    class _Stack:
        async def aclose(self):
            await asyncio.sleep(0.02)
            closed.append(True)

    conn = mc.McpConn(server={"id": 1}, client=object(), stack=_Stack())

    async def _cancelled_caller():
        try:
            await asyncio.sleep(3600)
        finally:
            await conn.aclose()

    task = asyncio.create_task(_cancelled_caller())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True], "aclose() was cut short by the cancellation"
