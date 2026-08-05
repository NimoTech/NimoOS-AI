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


def test_round_cap_is_the_sdk_default():
    """第二期声明了 elicitation，MRTR 循环可以靠自己走通，所以放开到 SDK 默认的 10。

    为什么不是交接文档原本建议的 3：mcp/client/_input_required.py:88-95 里，
    只带 requestState、不带 inputRequests 的响应**也算一轮**，而它不弹卡、不问用户，
    只 sleep 一下就重发（50ms 起、翻倍、250ms 封顶）。规范把这种响应当一等模式
    （服务端 MUST include at least one of inputRequests or requestState），
    它正是"授权还没完成，再来问"的标准表达。
    3 轮在 0.05+0.1+0.2 = 350ms 内就烧光，会对一个正常轮询的合规服务端直接抛
    InputRequiredRoundsExceededError —— 那是误伤，不是保护。

    轮次不进模型上下文、不消耗 agent turn：整个 MRTR 循环发生在 client.py 里
    那**一个** `await conn.call_tool(...)` 内部，SDK 消化掉全部中间轮次。
    1 → 10 不增加一个 token，成本纯粹是网络往返。
    """
    from mcp.client._input_required import DEFAULT_INPUT_REQUIRED_MAX_ROUNDS
    assert mc.MCP_INPUT_REQUIRED_ROUNDS == 10
    assert mc.MCP_INPUT_REQUIRED_ROUNDS == DEFAULT_INPUT_REQUIRED_MAX_ROUNDS


def test_rounds_exceeded_and_unsupported_capability_say_different_things():
    """两条分岔在第二期含义完全不同，共用一条文案会让模型给出错误建议。

    - 轮次耗尽：elicitation **已经支持**，问过用户、用户答了、服务端还是没就绪。
      正确的话是"去完成授权，然后重试"，不是"去改服务器配置"。
    - 缺失能力（sampling / roots）：我们确实没声明，"检查该 MCP 服务配置"仍然准确。
    """
    exceeded = mc._rounds_exceeded_msg("notion")
    unsupported = mc._unsupported_capability_msg("notion")

    assert exceeded != unsupported
    for msg in (exceeded, unsupported):
        assert "notion" in msg
        assert "do NOT retry with different arguments" in msg

    # 轮次耗尽这条不得再说"不支持"或"去改配置"
    assert "not supported" not in exceeded
    assert "configuration" not in exceeded
    assert "authorization" in exceeded.lower()

    # 缺失能力这条保留第一期措辞（对 sampling / roots 仍然准确）
    assert "not supported" in unsupported
    assert "configuration" in unsupported


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
async def test_rounds_exceeded_tells_the_model_to_wait_for_authorization():
    _setup_run(_RaisingConn(InputRequiredRoundsExceededError(10)))
    tool = mc._wrap_tool({"id": 1, "name": "notion"}, META)
    out = await tool.on_invoke_tool(None, json.dumps({}))

    assert "do NOT retry with different arguments" in out
    assert "notion" in out
    assert "authorization" in out.lower()
    assert "configuration" not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [
    MCPError(code=INVALID_REQUEST, message="Sampling not supported"),
    MCPError(code=INVALID_REQUEST, message="List roots not supported"),
    MCPError(code=MISSING_REQUIRED_CLIENT_CAPABILITY,
             message="this call needs a capability you did not declare",
             data={"requiredCapabilities": {"sampling": {}}}),
])
async def test_undeclared_capability_still_points_at_server_configuration(exc):
    """第二期仍然不声明 sampling / roots —— 这条路径必须原样活着。"""
    _setup_run(_RaisingConn(exc))
    tool = mc._wrap_tool({"id": 1, "name": "notion"}, META)
    out = await tool.on_invoke_tool(None, json.dumps({}))

    assert "needs interactive input" in out
    assert "do NOT retry with different arguments" in out
    assert "configuration" in out


@pytest.mark.asyncio
async def test_ordinary_failure_still_uses_the_generic_message():
    _setup_run(_RaisingConn(RuntimeError("upstream 502")))
    tool = mc._wrap_tool({"id": 1, "name": "notion"}, META)
    out = await tool.on_invoke_tool(None, json.dumps({}))

    assert "MCP tool search failed" in out
    assert "needs interactive input" not in out


# ── legacy(2025-11-25) 的 URL 授权是另一套机制 ─────────────────────────────────

def test_legacy_url_elicitation_code_comes_from_the_sdk():
    from mcp_types.jsonrpc import URL_ELICITATION_REQUIRED
    assert URL_ELICITATION_REQUIRED == -32042
    assert mc.URL_ELICITATION_REQUIRED is URL_ELICITATION_REQUIRED


def test_recognises_the_legacy_url_elicitation_error():
    """2025-11-25 用一个错误码要 URL 授权,2026-07-28 用 MRTR。前者还需要
    notifications/elicitation/complete 才能知道授权完成了,而客户端侧 SDK 根本没有
    这个通知的处理(mcp/client/ 与 mcp/shared/ grep 零命中)。所以我们不支持它 ——
    但要明说,不能静默掉进通用错误。"""
    from mcp.shared.exceptions import UrlElicitationRequiredError
    from mcp.types import ElicitRequestURLParams

    # 用 SDK 自己的异常类构造,而不是手搓一个 code=-32042 的 MCPError:
    # 这样 SDK 若改变 data 形状或码值,这条测试会红
    err = UrlElicitationRequiredError([ElicitRequestURLParams(
        message="Authorize", url="https://example.com/oauth",
        elicitationId="auth-001")])
    assert err.error.code == -32042
    assert mc._is_legacy_url_elicitation(err) is True
    # 它不该被前面那条"缺失能力"路径顺手认领
    assert mc._is_unsupported_capability(err) is False


def test_other_errors_are_not_mistaken_for_legacy_url_elicitation():
    assert mc._is_legacy_url_elicitation(
        MCPError(code=INVALID_REQUEST, message="bad")) is False
    assert mc._is_legacy_url_elicitation(RuntimeError("boom")) is False


@pytest.mark.asyncio
async def test_legacy_url_elicitation_gets_its_own_message():
    from mcp.shared.exceptions import UrlElicitationRequiredError
    from mcp.types import ElicitRequestURLParams

    _setup_run(_RaisingConn(UrlElicitationRequiredError([
        ElicitRequestURLParams(message="Authorize", url="https://example.com/oauth")])))
    tool = mc._wrap_tool({"id": 1, "name": "notion"}, META)
    out = await tool.on_invoke_tool(None, json.dumps({}))

    assert "notion" in out
    assert "do NOT retry with different arguments" in out
    assert "legacy" in out.lower()
    # 不得退化成第一期那两条里的任何一条,也不得掉进通用 "MCP tool ... failed"
    assert out != mc._rounds_exceeded_msg("notion")
    assert out != mc._unsupported_capability_msg("notion")
    assert "MCP tool search failed" not in out
