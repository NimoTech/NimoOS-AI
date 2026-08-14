import asyncio

import mcp_client.client as mc


def test_connect_uses_persisted_protocol_mode(monkeypatch):
    """A legacy HTTP server that silently discards server/discover makes
    mode="auto" wait out the full DISCOVER_TIMEOUT_SECONDS=10s before falling
    back (mcp/client/session.py:67), and our own cold-path budget is also
    10s -- every connection would necessarily hit that ceiling. Once the
    negotiated era is persisted, every connection after the first pays zero
    probing overhead."""
    seen = {}

    class _FakeClient:
        def __init__(self, transport, **kw):
            seen.update(kw)
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(mc, "Client", _FakeClient)
    monkeypatch.setattr(mc, "_build_transport",
                        lambda *a, **kw: asyncio.sleep(0, result=object()))

    asyncio.run(mc._connect({"id": 1, "name": "s", "transport": "http",
                             "url": "https://x", "protocol_mode": "legacy"}))
    assert seen["mode"] == "legacy", "a persisted era must skip the discover probe"


def test_connect_falls_back_to_auto_without_persisted_mode(monkeypatch):
    seen = {}

    class _FakeClient:
        def __init__(self, transport, **kw): seen.update(kw)
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(mc, "Client", _FakeClient)
    monkeypatch.setattr(mc, "_build_transport",
                        lambda *a, **kw: asyncio.sleep(0, result=object()))
    asyncio.run(mc._connect({"id": 1, "name": "s", "transport": "http", "url": "https://x"}))
    assert seen["mode"] == "auto"


def test_connect_falls_back_to_auto_for_unrecognized_persisted_mode(monkeypatch, caplog):
    """Go persists whatever protocol version it negotiated. If a future SDK
    upgrade drops that version from its own MODERN_PROTOCOL_VERSIONS, blindly
    passing the stale value through to Client(mode=...) would raise at
    construction time on every single connection attempt -- with no
    self-healing path, since nothing here re-probes on its own (Go is the
    sole writer of protocol_mode). A stored value the SDK no longer
    recognizes must fall back to "auto" instead, and the fallback must be
    logged so a stale pin is diagnosable rather than silently swallowed."""
    seen = {}

    class _FakeClient:
        def __init__(self, transport, **kw): seen.update(kw)
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(mc, "Client", _FakeClient)
    monkeypatch.setattr(mc, "_build_transport",
                        lambda *a, **kw: asyncio.sleep(0, result=object()))
    with caplog.at_level("WARNING"):
        asyncio.run(mc._connect({"id": 1, "name": "s", "transport": "http",
                                 "url": "https://x", "protocol_mode": "2099-01-01"}))
    assert seen["mode"] == "auto"
    assert "2099-01-01" in caplog.text


def test_probe_always_renegotiates_with_auto_even_with_a_persisted_mode(monkeypatch):
    """The /test probe (test_server -> _test_server_inner) is the one call that
    PRODUCES the persisted protocol_mode everything else reuses -- it must
    never inherit a stale pin from a previous probe, or a wrong/outdated era
    could never self-correct (Go is the sole writer of protocol_mode, and
    nothing here re-probes on its own outside of /test). So even when the
    server dict being (re-)tested already carries a protocol_mode from an
    earlier probe, _connect must still be called with mode="auto"."""
    seen = {}

    class _FakeConn:
        async def list_tools(self):
            return [], mc.SCHEMA_TTL
        async def aclose(self): pass

    async def fake_connect(server, connect_timeout=None, mode=None):
        seen["mode"] = mode
        return _FakeConn()

    monkeypatch.setattr(mc, "_connect", fake_connect)
    out = asyncio.run(mc.test_server({"id": 1, "name": "s", "transport": "http",
                                      "url": "https://x", "protocol_mode": "legacy"}))
    assert out["ok"] is True
    assert seen["mode"] == "auto", (
        "the probe must re-negotiate from scratch, not inherit a stale persisted era")
