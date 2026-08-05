"""MRTR (server asks the client for more input) — phase 1 behaviour.

We register no elicitation/sampling/roots callbacks, so the SDK does not declare
those capabilities and a compliant server will never ask. Two gaps remain, and they
surface as TWO DIFFERENT exceptions — hence two branches, not one catch-all:

  gap 1  InputRequiredResult carrying only requestState ("warming up, retry"):
         the SDK retries by itself; with max_rounds=1 a second one throws
         InputRequiredRoundsExceededError.
  gap 2  a non-compliant server sends inputRequests anyway: the SDK's built-in
         default callback answers ErrorData(INVALID_REQUEST, "... not supported")
         and _dispatch_all raises MCPError on round ONE — it never reaches the cap.
  gap 3  a COMPLIANT server refuses up front with MISSING_REQUIRED_CLIENT_CAPABILITY
         (-32021) + data.requiredCapabilities. This is the shape we actually meet in
         the field, and it must reach the same "needs interactive input" message —
         otherwise the model gets a generic failure and may keep retrying a call that
         can never succeed.
"""
import json

import pytest
from mcp.client import InputRequiredRoundsExceededError
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_REQUEST, INTERNAL_ERROR
from mcp_types.jsonrpc import MISSING_REQUIRED_CLIENT_CAPABILITY

import mcp_client.client as mc

META = {"name": "search", "description": "d",
        "input_schema": {"type": "object", "properties": {}}}


def test_round_cap_is_one():
    assert mc.MCP_INPUT_REQUIRED_ROUNDS == 1


@pytest.mark.parametrize("message", [
    "Elicitation not supported",
    "Sampling not supported",
    "List roots not supported",
])
def test_recognises_undeclared_capability(message):
    err = MCPError(code=INVALID_REQUEST, message=message)
    assert mc._is_unsupported_capability(err) is True


def test_recognises_compliant_missing_capability_refusal():
    """gap 3: a compliant 2026-07-28 server refuses with -32021 rather than asking a
    client that never declared the capability. Verified against a real test server whose
    reply was `{"code": -32021, "message": "greet needs to elicit a name from the user",
    "data": {"requiredCapabilities": {"elicitation": {"form": {}}}}}`.

    The server's own message carries no "not supported" suffix, so ONLY the code can
    classify this — which is fine: -32021 is a dedicated code meaning exactly this.
    """
    err = MCPError(code=MISSING_REQUIRED_CLIENT_CAPABILITY,
                   message="greet needs to elicit a name from the user",
                   data={"requiredCapabilities": {"elicitation": {"form": {}}}})
    assert mc._is_unsupported_capability(err) is True


def test_missing_capability_code_comes_from_the_sdk():
    """Pin the numeric code to the SDK constant rather than hardcoding -32021 here, so
    a spec/SDK renumbering surfaces as a failure instead of silent misclassification."""
    assert MISSING_REQUIRED_CLIENT_CAPABILITY == -32021
    assert mc.MISSING_REQUIRED_CLIENT_CAPABILITY is MISSING_REQUIRED_CLIENT_CAPABILITY


def test_sdk_sentinel_messages_are_pinned():
    """Pin the SDK's own wording. If a future SDK rephrases these, this test must
    FAIL — silently falling through to the generic error branch would turn a clear
    'this server needs interactive input' message back into noise."""
    from mcp.client import session as sdk_session

    src = __import__("inspect").getsource(sdk_session)
    for sentinel in ("Elicitation not supported", "Sampling not supported",
                     "List roots not supported"):
        assert sentinel in src, f"SDK no longer emits {sentinel!r} — update _is_unsupported_capability"


def test_business_failure_is_not_mistaken_for_a_capability_problem():
    """A remote tool's business failure comes back as isError=True inside the result,
    not as a JSON-RPC error. Anything that does arrive as an error with a different
    code must keep going down the generic branch."""
    assert mc._is_unsupported_capability(
        MCPError(code=INTERNAL_ERROR, message="upstream not supported")) is False
    assert mc._is_unsupported_capability(
        MCPError(code=INVALID_REQUEST, message="bad argument foo")) is False
    assert mc._is_unsupported_capability(RuntimeError("boom")) is False


def _setup_run(conn):
    import sqlite3

    from confirm import ConfirmManager

    sconn = sqlite3.connect(":memory:")
    sconn.execute("CREATE TABLE pending_confirmations (confirm_id TEXT, session_id TEXT, "
                  "action TEXT, description TEXT, command TEXT, created_at INT)")
    mgr = ConfirmManager(sconn, timeout=5)
    mc.SESSION_ID_VAR.set("s1")
    mc.EVENT_QUEUE_VAR.set(None)
    mc.CONFIRM_MGR_VAR.set(mgr)
    mc.USER_PATTERNS_VAR.set([])
    mc._CONFIRMED_TOOLS_VAR.set({"1::search"})     # pre-approved: skip the confirm card
    mc._RUN_CONNS_VAR.set({1: conn})
    mc._RUN_CONN_LOCKS_VAR.set({})


class _RaisingConn:
    def __init__(self, exc): self._exc = exc
    async def call_tool(self, name, args): raise self._exc
    async def aclose(self): pass


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [
    InputRequiredRoundsExceededError("too many rounds"),
    MCPError(code=INVALID_REQUEST, message="Elicitation not supported"),
    MCPError(code=MISSING_REQUIRED_CLIENT_CAPABILITY,
             message="greet needs to elicit a name from the user",
             data={"requiredCapabilities": {"elicitation": {"form": {}}}}),
])
async def test_all_gaps_produce_the_same_do_not_retry_message(exc):
    _setup_run(_RaisingConn(exc))
    tool = mc._wrap_tool({"id": 1, "name": "notion"}, META)
    out = await tool.on_invoke_tool(None, json.dumps({}))

    assert "needs interactive input" in out
    assert "do NOT retry with different arguments" in out
    assert "notion" in out


@pytest.mark.asyncio
async def test_ordinary_failure_still_uses_the_generic_message():
    _setup_run(_RaisingConn(RuntimeError("upstream 502")))
    tool = mc._wrap_tool({"id": 1, "name": "notion"}, META)
    out = await tool.on_invoke_tool(None, json.dumps({}))

    assert "MCP tool search failed" in out
    assert "needs interactive input" not in out
