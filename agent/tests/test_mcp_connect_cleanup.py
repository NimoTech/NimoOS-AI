"""Connect-path resource cleanup.

_connect enters the SDK Client into an AsyncExitStack. Every caller wraps it in
asyncio.wait_for, so the most common failure is a CancelledError arriving mid
handshake — the cleanup must therefore be shielded, or the httpx client / unix
socket is never released.

Both tests below use anyio.move_on_after, NOT asyncio.wait_for/Task.cancel(). A
plain asyncio Task.cancel() delivers exactly one CancelledError at the next
checkpoint; if that checkpoint happens to be masked (e.g. by a shield) asyncio
does not automatically re-deliver it, so a test built on Task.cancel() can pass
whether or not the shield is actually there — it never proves anything. anyio's
CancelScope instead keeps re-injecting cancellation at EVERY checkpoint reached
while the scope's deadline has passed, until the scope itself is exited — the
same mechanism the SDK's own internal anyio task groups use, so this is what
actually reproduces the hazard the shield protects against. This was confirmed
empirically via a mutation check on the production code (temporarily deleting
`shield=True` turns both tests red; restoring it turns them green — output
captured in task-3-report.md, fix round 2).
"""
import anyio
import pytest

import mcp_client.client as mc


class _HangingClient:
    """Stands in for mcp.client.Client: an async CM that never finishes __aenter__."""

    async def __aenter__(self):
        await anyio.sleep(3600)
        return self

    async def __aexit__(self, *exc):
        return False


async def _record_closed(sink):
    # A real suspension point. Without one here, this callback would run to
    # completion in zero checkpoints and a missing shield would never get a
    # chance to interrupt it — the mutation (deleting shield=True) would go
    # undetected, which is exactly the bug the previous version of this test had.
    await anyio.sleep(0.05)
    sink.append(True)


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

    # The deadline fires while we're suspended inside _HangingClient.__aenter__'s
    # anyio.sleep(3600); anyio keeps delivering that cancellation at every
    # checkpoint downstream (including inside _record_closed's own sleep) unless
    # something shields it — which is exactly what _connect's except-clause does.
    with anyio.move_on_after(0.05):
        await mc._connect({"id": 1, "name": "x", "transport": "http", "url": "https://x"})

    # transport cleanup callback ran to completion despite the still-active outer
    # cancellation — proves the shield actually blocked the cancel from reaching it.
    assert closed == [True], "connect cleanup was cut short by cancellation — resources leaked"


@pytest.mark.asyncio
async def test_aclose_is_cancel_shielded():
    """McpConn.aclose() runs inside _cold_fetch's finally, which is itself inside
    an outer wait_for. A bare await there would be re-cancelled immediately."""
    closed = []

    class _Stack:
        async def aclose(self):
            await anyio.sleep(0.05)   # real suspension point cancellation can land on
            closed.append(True)

    conn = mc.McpConn(server={"id": 1}, client=object(), stack=_Stack())

    # Same mechanism as above: the 0.01s deadline has already passed by the time
    # we're suspended inside _Stack.aclose()'s sleep, so anyio tries to deliver
    # cancellation right there. Only aclose()'s own shield can stop it.
    with anyio.move_on_after(0.01):
        await conn.aclose()

    assert closed == [True], "aclose() was cut short by the cancellation"
