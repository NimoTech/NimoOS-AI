import asyncio

import pytest
import mcp_client.client as mc


class GoodConn:
    """Matches the new McpConn contract: list_tools / aclose."""
    async def list_tools(self):
        return [{"name": "search", "description": "d",
                 "input_schema": {"type": "object", "properties": {}}}], mc.SCHEMA_TTL
    async def aclose(self): pass


@pytest.fixture(autouse=True)
def _clear():
    mc._SCHEMA_CACHE.clear(); yield; mc._SCHEMA_CACHE.clear()


@pytest.mark.asyncio
async def test_test_server_ok_and_warms_cache(monkeypatch):
    async def fake_connect(s, connect_timeout=None): return GoodConn()
    monkeypatch.setattr(mc, "_connect", fake_connect)
    out = await mc.test_server({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert out["ok"] is True and out["tool_count"] == 1 and out["tools"] == ["search"]
    assert mc._cache_get(1) is not None        # warmed


@pytest.mark.asyncio
async def test_test_server_connect_failure(monkeypatch):
    async def boom(s, connect_timeout=None): raise RuntimeError("refused")
    monkeypatch.setattr(mc, "_connect", boom)
    out = await mc.test_server({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert out["ok"] is False
    assert out["error_key"] == "connect_failed"
    assert "Connection failed" in out["error"]
    assert out["detail"] == "refused"


@pytest.mark.asyncio
async def test_test_server_overall_timeout(monkeypatch):
    monkeypatch.setattr(mc, "TEST_TIMEOUT", 0.05)
    async def slow_connect(s, connect_timeout=None):
        import asyncio
        await asyncio.sleep(1)
    monkeypatch.setattr(mc, "_connect", slow_connect)
    out = await mc.test_server({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert out["ok"] is False
    assert out["error_key"] == "probe_timeout"
    assert out["error"] == "Probe timed out"


@pytest.mark.asyncio
async def test_test_server_overall_timeout_still_closes_conn(monkeypatch):
    """Regression: the OUTER test_server wait_for(budget) deadline is always set
    before the INNER asyncio.wait_for(conn.list_tools(), budget) deadline -- the
    inner one only starts after connect() returns, so even with an identical
    budget its absolute deadline is strictly later. That means a slow list_tools
    times out via the OUTER wait_for, which delivers a CancelledError (a
    BaseException) into _test_server_inner -- neither its
    `except asyncio.TimeoutError` nor its `except Exception` catches that, so
    without a try/finally around the inner wait_for, conn.aclose() is never
    reached and the whole McpConn (httpx2 client / unix socket / stdio bridge)
    leaks. Must go through test_server (not call _test_server_inner directly)
    so the OUTER deadline is the one that actually fires -- calling
    _test_server_inner directly (as test_test_server_list_tools_timeout below
    does) only ever exercises the INNER timeout, which was never broken.
    """
    closed = []

    class SlowListConn:
        async def list_tools(self):
            await asyncio.sleep(10)  # much longer than TEST_TIMEOUT below
        async def aclose(self):
            closed.append(True)

    async def fake_connect(s, connect_timeout=None):
        return SlowListConn()

    monkeypatch.setattr(mc, "_connect", fake_connect)
    monkeypatch.setattr(mc, "TEST_TIMEOUT", 0.05)
    out = await mc.test_server({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert out["ok"] is False
    assert out["error_key"] == "probe_timeout"
    assert closed == [True], "conn.aclose() must be called even when the OUTER test_server deadline fires"


@pytest.mark.asyncio
async def test_test_server_list_tools_timeout(monkeypatch):
    # Drive _test_server_inner directly (not the outer test_server wait_for) so the
    # inner list_tools budget can be shrunk without racing the overall probe timeout.
    class SlowListSrv:
        async def list_tools(self):
            import asyncio
            await asyncio.sleep(1)
        async def aclose(self): pass

    async def fake_connect(s, connect_timeout=None):
        return SlowListSrv()
    monkeypatch.setattr(mc, "_connect", fake_connect)
    monkeypatch.setattr(mc, "PROBE_LIST_TIMEOUT", 0.05)
    out = await mc._test_server_inner({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert out["ok"] is False
    assert out["error_key"] == "list_timeout"
    assert out["error"] == "Listing tools timed out"


@pytest.mark.asyncio
async def test_test_server_list_tools_failure(monkeypatch):
    class BoomListSrv:
        async def list_tools(self): raise RuntimeError("bad response")
        async def aclose(self): pass

    async def fake_connect(s, connect_timeout=None):
        return BoomListSrv()
    monkeypatch.setattr(mc, "_connect", fake_connect)
    out = await mc._test_server_inner({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert out["ok"] is False
    assert out["error_key"] == "list_failed"
    assert "Listing tools failed" in out["error"]
    assert out["detail"] == "bad response"


def test_probe_budget_invariant_holds_for_both_transports():
    """The outer backstop must never truncate a phase still inside its own budget.

    When this invariant breaks the symptom is "the server connects fine but the probe
    reports probe_timeout", it only shows up on slow servers, and it is miserable to
    track down. Two independently editable numbers on each side, so pin the relation.
    """
    assert mc.TEST_TIMEOUT >= mc.PROBE_CONNECT_TIMEOUT + mc.PROBE_LIST_TIMEOUT
    assert mc.STDIO_TEST_TIMEOUT >= mc.STDIO_PROBE_CONNECT_TIMEOUT + mc.STDIO_PROBE_LIST_TIMEOUT


def test_connect_budget_leaves_room_for_the_legacy_fallback():
    """mode='auto' spends DISCOVER_TIMEOUT_SECONDS on discover before falling back to
    initialize. The connect budget has to hold both legs, or a stalled modern probe
    means the legacy handshake never gets to run -- which is precisely the case this
    feature exists to report. Read the real SDK constant; never hardcode 10.
    """
    from mcp.client.session import DISCOVER_TIMEOUT_SECONDS
    assert mc.PROBE_CONNECT_TIMEOUT > DISCOVER_TIMEOUT_SECONDS
    assert mc.STDIO_PROBE_CONNECT_TIMEOUT > DISCOVER_TIMEOUT_SECONDS


@pytest.mark.parametrize("transport,connect_c,list_c", [
    ("http", "PROBE_CONNECT_TIMEOUT", "PROBE_LIST_TIMEOUT"),
    ("stdio", "STDIO_PROBE_CONNECT_TIMEOUT", "STDIO_PROBE_LIST_TIMEOUT"),
])
def test_phase_helpers_pick_the_transport_specific_budget(transport, connect_c, list_c):
    s = {"transport": transport}
    assert mc._probe_connect_timeout(s) == getattr(mc, connect_c)
    assert mc._probe_list_timeout(s) == getattr(mc, list_c)


@pytest.mark.asyncio
async def test_connect_phase_has_its_own_timeout(monkeypatch):
    """The connect phase reports its own error_key, not the outer probe_timeout.

    Only the connect budget is shrunk here; the outer backstop keeps its default, which
    is what proves the INNER wait_for is the one firing.

    This also pins the order of the two except clauses. In Python 3.11
    asyncio.TimeoutError IS the builtin TimeoutError, which subclasses OSError and thus
    Exception -- so `except Exception` placed first would swallow the timeout and
    silently downgrade it to connect_failed. This assertion goes red if that happens.
    """
    monkeypatch.setattr(mc, "PROBE_CONNECT_TIMEOUT", 0.05)

    async def slow_connect(s, connect_timeout=None):
        await asyncio.sleep(5)
    monkeypatch.setattr(mc, "_connect", slow_connect)

    out = await mc.test_server({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert out["ok"] is False
    assert out["error_key"] == "connect_timeout"
    assert out["error"] == "Connection timed out"


@pytest.mark.asyncio
async def test_list_phase_budget_is_independent_of_the_connect_budget(monkeypatch):
    """The list phase reads its own constant: leave connect wide, squeeze only list,
    and list_timeout must still come back."""
    monkeypatch.setattr(mc, "PROBE_CONNECT_TIMEOUT", 30)
    monkeypatch.setattr(mc, "PROBE_LIST_TIMEOUT", 0.05)

    class SlowList:
        async def list_tools(self):
            await asyncio.sleep(5)
        async def aclose(self): pass

    async def fake_connect(s, connect_timeout=None): return SlowList()
    monkeypatch.setattr(mc, "_connect", fake_connect)

    out = await mc._test_server_inner({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert out["ok"] is False
    assert out["error_key"] == "list_timeout"


@pytest.mark.asyncio
async def test_connect_still_receives_the_stdio_enforced_budget(monkeypatch):
    """The outer wait_for does not replace the connect_timeout= argument: that argument
    is the only bound actually enforced on the stdio branch (netns start_mcp_stdio).
    Both have to be present."""
    seen = {}

    class OK:
        async def list_tools(self): return [], mc.SCHEMA_TTL
        async def aclose(self): pass
        def protocol_info(self): return {}

    async def fake_connect(s, connect_timeout=None):
        seen["ct"] = connect_timeout
        return OK()
    monkeypatch.setattr(mc, "_connect", fake_connect)

    await mc.test_server({"id": 1, "name": "x", "transport": "stdio", "command": "npx"})
    assert seen["ct"] == mc.STDIO_PROBE_CONNECT_TIMEOUT
