import asyncio
import mcp_client.client as mc


class _FakeTool:
    def __init__(self, name, description, input_schema):
        self.name = name
        self.description = description
        self.input_schema = input_schema


class _FakeResult:
    def __init__(self, tools, ttl_ms=0):
        self.tools = tools
        self.ttl_ms = ttl_ms


class _FakeSession:
    discover_result = None

    @property
    def server_info(self):
        class _I:
            name, title, version, description = "github-mcp-server", "GitHub", "0.6.2", "d"
        return _I()

    initialize_result = None


class _FakeClient:
    protocol_version = "2025-11-25"
    session = _FakeSession()

    async def list_tools(self):
        return _FakeResult([_FakeTool("create_issue", "Create an issue", {"type": "object"})], 600000)


class _FakeConn:
    def __init__(self):
        self.client = _FakeClient()

    async def list_tools(self):
        metas = [mc._extract_meta(t) for t in (await self.client.list_tools()).tools]
        return metas, 600

    def protocol_info(self):
        return {"protocol_era": "legacy", "protocol_version": "2025-11-25",
                "supported_versions": ["2025-11-25"]}

    async def aclose(self):
        pass


def test_test_server_returns_identity_card(monkeypatch):
    async def fake_connect(server, connect_timeout=None, mode=None):
        return _FakeConn()
    monkeypatch.setattr(mc, "_connect", fake_connect)
    monkeypatch.setattr(mc, "_read_instructions", lambda conn: "Tools for GitHub.")
    monkeypatch.setattr(mc, "_read_server_info", lambda conn: {
        "name": "github-mcp-server", "title": "GitHub", "version": "0.6.2", "description": "d"})

    res = asyncio.run(mc.test_server({"id": 1, "name": "gh", "transport": "http", "url": "https://x"}))

    assert res["ok"] is True
    assert res["instructions"] == "Tools for GitHub."
    assert res["server_info"]["name"] == "github-mcp-server"
    assert res["ttl_sec"] == 600
    assert res["tool_metas"][0]["name"] == "create_issue"
    assert len(res["tool_metas"][0]["schema_hash"]) == 16
    assert len(res["tool_metas"][0]["desc_hash"]) == 16
    # Schema bodies must be returned too: Go persists them, and Python later
    # reads them back via loopback instead of ever reconnecting to the third party.
    assert res["schemas"][0]["input_schema"] == {"type": "object"}
    assert res["schemas"][0]["description"] == "Create an issue"


def test_identity_readers_never_fail_the_probe(monkeypatch):
    """A failed identity read must not turn an otherwise successful probe into
    a failure -- it is only nice-to-have metadata."""
    class _Boom:
        client = None
        def protocol_info(self):
            return {"protocol_era": "legacy", "protocol_version": "x", "supported_versions": []}
    assert mc._read_instructions(_Boom()) == ""
    assert mc._read_server_info(_Boom()) == {}
