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
async def test_test_server_ok_and_does_not_warm_the_schema_cache(monkeypatch):
    """test_server is a pure probe now: Go is the sole writer of probe results
    (it persists the identity card / tool metas / schemas this call returns),
    so Python must not also keep its own parallel in-memory copy."""
    async def fake_connect(s, connect_timeout=None): return GoodConn()
    monkeypatch.setattr(mc, "_connect", fake_connect)
    out = await mc.test_server({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert out["ok"] is True and out["tool_count"] == 1 and out["tools"] == ["search"]
    assert mc._cache_get(1) is None            # not warmed


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
    # The close phase counts too: _test_server_inner's `finally: await conn.aclose()`
    # runs inside the outer wait_for and is bounded by MCP_CLOSE_TIMEOUT, so a slow
    # teardown on an otherwise successful probe must not be able to exhaust the
    # backstop and throw the already-computed result away.
    assert mc.TEST_TIMEOUT >= (
        mc.PROBE_CONNECT_TIMEOUT + mc.PROBE_LIST_TIMEOUT + mc.MCP_CLOSE_TIMEOUT)
    assert mc.STDIO_TEST_TIMEOUT >= (
        mc.STDIO_PROBE_CONNECT_TIMEOUT + mc.STDIO_PROBE_LIST_TIMEOUT + mc.MCP_CLOSE_TIMEOUT)


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


class _FakeDiscover:
    def __init__(self, versions): self.supported_versions = versions


class _FakeSession:
    def __init__(self, discover_result): self.discover_result = discover_result


class _FakeClient:
    def __init__(self, version, discover_result):
        self.protocol_version = version
        self.session = _FakeSession(discover_result)


def _conn_with(version, discover_result):
    return mc.McpConn(server={"id": 1}, client=_FakeClient(version, discover_result), stack=None)


def test_protocol_info_modern_reports_the_servers_own_version_list():
    conn = _conn_with("2026-07-28", _FakeDiscover(["2026-07-28", "2025-11-25"]))
    assert conn.protocol_info() == {
        "protocol_era": "modern",
        "protocol_version": "2026-07-28",
        "supported_versions": ["2026-07-28", "2025-11-25"],
    }


def test_protocol_info_legacy_reports_only_the_negotiated_revision():
    """Legacy has no enumeration primitive. Never infer "it also supports the earlier
    revisions" from protocol_version."""
    conn = _conn_with("2025-06-18", None)
    assert conn.protocol_info() == {
        "protocol_era": "legacy",
        "protocol_version": "2025-06-18",
        "supported_versions": ["2025-06-18"],
    }


def test_protocol_info_copies_the_sdk_list():
    """The returned list must be a copy: it gets serialized into the HTTP response and
    must not be a live reference into the SDK result object."""
    dr = _FakeDiscover(["2026-07-28"])
    out = _conn_with("2026-07-28", dr).protocol_info()
    out["supported_versions"].append("tampered")
    assert dr.supported_versions == ["2026-07-28"]


@pytest.mark.asyncio
async def test_test_server_surfaces_the_protocol_fields(monkeypatch):
    class ModernConn:
        async def list_tools(self): return [{"name": "t", "description": "", "input_schema": {}}], mc.SCHEMA_TTL
        async def aclose(self): pass
        def protocol_info(self):
            return {"protocol_era": "modern", "protocol_version": "2026-07-28",
                    "supported_versions": ["2026-07-28", "2025-11-25"]}

    async def fake_connect(s, connect_timeout=None): return ModernConn()
    monkeypatch.setattr(mc, "_connect", fake_connect)

    out = await mc.test_server({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert out["ok"] is True
    assert out["protocol_era"] == "modern"
    assert out["supported_versions"] == ["2026-07-28", "2025-11-25"]


@pytest.mark.asyncio
async def test_a_broken_version_readout_never_fails_the_probe(monkeypatch):
    """The server is fine and the tools listed; only the version readout broke -- the
    result must still be ok:True. Hard constraint: a purely cosmetic field must never
    reduce the availability of the connectivity test."""
    class WeirdConn:
        async def list_tools(self): return [{"name": "t", "description": "", "input_schema": {}}], mc.SCHEMA_TTL
        async def aclose(self): pass
        def protocol_info(self): raise RuntimeError("sdk moved the attribute")

    async def fake_connect(s, connect_timeout=None): return WeirdConn()
    monkeypatch.setattr(mc, "_connect", fake_connect)

    out = await mc.test_server({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert out["ok"] is True and out["tool_count"] == 1
    assert out["protocol_era"] == "unknown"
    assert out["supported_versions"] == []


@pytest.mark.asyncio
async def test_protocol_fields_are_read_before_the_connection_closes(monkeypatch):
    """Read order matters: the SDK's session attributes are not guaranteed readable
    after aclose()."""
    order = []

    class OrderConn:
        async def list_tools(self): return [], mc.SCHEMA_TTL
        async def aclose(self): order.append("close")
        def protocol_info(self):
            order.append("read")
            return {"protocol_era": "legacy", "protocol_version": "2025-11-25",
                    "supported_versions": ["2025-11-25"]}

    async def fake_connect(s, connect_timeout=None): return OrderConn()
    monkeypatch.setattr(mc, "_connect", fake_connect)

    await mc.test_server({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert order == ["read", "close"]


@pytest.mark.asyncio
async def test_test_server_result_carries_no_cache_entry_shape(monkeypatch):
    """test_server's return value mixes protocol metadata (protocol_era, ...) with
    the tool manifest (tool_metas, schemas, ...) in one dict -- that is fine for a
    JSON response, but it must never be mistaken for (or fed into) a _CacheEntry.
    test_server no longer touches _SCHEMA_CACHE at all; the schema cache is only
    ever warmed by the run-start cold path (_cold_fetch/_revalidate), which stores
    metas alone, never protocol fields."""
    class ModernConn:
        async def list_tools(self): return [{"name": "t", "description": "", "input_schema": {}}], mc.SCHEMA_TTL
        async def aclose(self): pass
        def protocol_info(self):
            return {"protocol_era": "modern", "protocol_version": "2026-07-28",
                    "supported_versions": ["2026-07-28"]}

    async def fake_connect(s, connect_timeout=None): return ModernConn()
    monkeypatch.setattr(mc, "_connect", fake_connect)

    out = await mc.test_server({"id": 1, "name": "x", "transport": "http", "url": "https://x"})
    assert out["protocol_era"] == "modern"
    assert out["tool_metas"][0]["name"] == "t"
    assert mc._cache_get(1) is None
