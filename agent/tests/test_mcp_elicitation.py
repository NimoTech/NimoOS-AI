"""elicitation 回调：把服务端的提问变成一张卡,把用户的答复变回 ElicitResult。

本文件末尾那三条端到端测试跑在**真实 SDK 对象**上（真 Server / 真 Client / 真
InMemoryTransport / 真 JSON-RPC 编解码），这是交接文档 §8 明确要求的：假对象上
camelCase 属性是真实存在的,而 mcp 2.0 把字段改成了 snake_case,camelCase 只作 wire
alias —— 第一期因此中过两次招（inputSchema / isError）。

脚手架注意（实测踩过）：Client 在 call_tool 末尾会调 validate_tool_result,那会回头
调 list_tools()。所以内存流对里的测试 Server **必须同时提供 on_list_tools**,否则
call_tool 收尾时抛 "Method not found"。
"""
import asyncio
import sqlite3
from contextlib import AsyncExitStack
from contextvars import ContextVar

import mcp.types as mtypes
import mcp_types as T
import pytest
from mcp.client import Client
from mcp.client._memory import InMemoryTransport
from mcp.server.lowlevel import Server

import mcp_client.client as mc
from confirm import ConfirmManager
from mcp_client.elicitation import make_elicitation_callback

SERVER = {"id": 1, "name": "notion"}

FORM_SCHEMA = {"type": "object",
               "properties": {"name": {"type": "string", "title": "Name"}},
               "required": ["name"]}


def _mgr():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE pending_confirmations (confirm_id TEXT, session_id TEXT, "
               "action TEXT, description TEXT, command TEXT, created_at INT)")
    return ConfirmManager(db, timeout=5), db


def _ctx(mgr, queue, session_id="s1"):
    """回调按 ContextVar 读运行期上下文,这里造一组独立的 ContextVar 传进去,
    免得测试之间互相污染 mcp_client.client 的模块级变量。"""
    sv, qv, mv = (ContextVar("s", default=""), ContextVar("q", default=None),
                  ContextVar("m", default=None))
    sv.set(session_id)
    qv.set(queue)
    mv.set(mgr)
    return {"session_id_var": sv, "queue_var": qv, "mgr_var": mv}


# ── 回调单元行为 ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_form_request_emits_a_card_with_rendered_fields():
    mgr, _ = _mgr()
    queue = asyncio.Queue()
    cb = make_elicitation_callback(SERVER, **_ctx(mgr, queue))
    params = T.ElicitRequestFormParams(message="Who are you?",
                                       requestedSchema=FORM_SCHEMA)

    task = asyncio.create_task(cb(None, params))
    event = await asyncio.wait_for(queue.get(), timeout=2)

    assert event["type"] == "confirmation_required"
    assert event["kind"] == "mcp_elicit_form"
    assert event["server"] == "notion"
    assert event["message"] == "Who are you?"
    assert [f["key"] for f in event["fields"]] == ["name"]
    assert event["fields"][0]["required"] is True
    assert event["confirm_id"]
    assert event["error"] is None          # 首次提问没有"上次被退回"的原因
    # 卡片事件里绝不能出现答案槽位 —— 它会进 event_log
    assert "content" not in event

    mgr.resolve(event["confirm_id"], True, action="accept", content={"name": "Nimo"})
    result = await asyncio.wait_for(task, timeout=2)
    assert result.action == "accept"
    assert result.content == {"name": "Nimo"}


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["decline", "cancel"])
async def test_refusal_is_an_ElicitResult_never_ErrorData(action):
    """硬规则：_dispatch_all 里第一个返回 ErrorData 的任务会 cancel 掉同一轮里所有
    兄弟任务。用 ErrorData 拒绝 = 炸掉用户已经填好的其它卡。"""
    from mcp.types import ElicitResult, ErrorData

    mgr, _ = _mgr()
    queue = asyncio.Queue()
    cb = make_elicitation_callback(SERVER, **_ctx(mgr, queue))
    task = asyncio.create_task(cb(None, T.ElicitRequestFormParams(
        message="m", requestedSchema=FORM_SCHEMA)))
    event = await asyncio.wait_for(queue.get(), timeout=2)

    mgr.resolve(event["confirm_id"], False, action=action)
    result = await asyncio.wait_for(task, timeout=2)

    assert isinstance(result, ElicitResult)
    assert not isinstance(result, ErrorData)
    assert result.action == action
    assert result.content is None


@pytest.mark.asyncio
async def test_no_run_context_declines_instead_of_hanging():
    """_cold_fetch / _revalidate / test_server 的连接没有浏览器可问。
    挂住会把一次 schema 预取变成一个永不返回的后台任务。"""
    from mcp.types import ElicitResult

    cb = make_elicitation_callback(SERVER, **_ctx(None, None, session_id=""))
    result = await asyncio.wait_for(
        cb(None, T.ElicitRequestFormParams(message="m", requestedSchema=FORM_SCHEMA)),
        timeout=2)
    assert isinstance(result, ElicitResult) and result.action == "decline"


@pytest.mark.asyncio
async def test_an_invalid_answer_is_re_asked_not_burned():
    """SDK 对 requestedSchema 零校验,校验是我们的活 —— 但一次性 decline 会**烧掉**
    用户的答案：confirm_id 已消费、卡片已 resolve,没有任何回到"再填一次"的路,
    用户只能让 agent 把整个工具调用重来。所以改成带着原因重问。

    重问在协议上是免费的：_dispatch_all 对回调不加超时,轮次计数器在我们 await
    期间不动,60 秒 read timeout 是每轮的、发生在轮次之间。
    """
    mgr, _ = _mgr()
    queue = asyncio.Queue()
    cb = make_elicitation_callback(SERVER, **_ctx(mgr, queue))
    task = asyncio.create_task(cb(None, T.ElicitRequestFormParams(
        message="m", requestedSchema=FORM_SCHEMA)))

    first = await asyncio.wait_for(queue.get(), timeout=2)
    assert first["error"] is None
    mgr.resolve(first["confirm_id"], True, action="accept", content={"name": 12345})

    second = await asyncio.wait_for(queue.get(), timeout=2)
    assert second["kind"] == "mcp_elicit_form"
    assert second["confirm_id"] != first["confirm_id"]     # 全新的待办,可以再答
    assert second["error"] and "Name" in second["error"]   # 卡片说得出为什么被退
    assert "content" not in second                         # 上次的答案不回填(会进 event_log)

    mgr.resolve(second["confirm_id"], True, action="accept", content={"name": "Nimo"})
    result = await asyncio.wait_for(task, timeout=2)
    assert result.action == "accept" and result.content == {"name": "Nimo"}


@pytest.mark.asyncio
async def test_re_asking_is_bounded_so_an_unsatisfiable_schema_cannot_loop_forever():
    from mcp_client.elicitation import MAX_ANSWER_ATTEMPTS

    mgr, _ = _mgr()
    queue = asyncio.Queue()
    cb = make_elicitation_callback(SERVER, **_ctx(mgr, queue))
    task = asyncio.create_task(cb(None, T.ElicitRequestFormParams(
        message="m", requestedSchema=FORM_SCHEMA)))

    cards = []
    for _ in range(MAX_ANSWER_ATTEMPTS):
        card = await asyncio.wait_for(queue.get(), timeout=2)
        cards.append(card)
        mgr.resolve(card["confirm_id"], True, action="accept", content={"name": 1})

    result = await asyncio.wait_for(task, timeout=2)
    assert len(cards) == MAX_ANSWER_ATTEMPTS
    assert result.action == "decline"
    warning = await asyncio.wait_for(queue.get(), timeout=2)
    assert warning["type"] == "mcp_warning" and warning["server"] == "notion"


@pytest.mark.asyncio
async def test_declining_a_re_asked_card_stops_immediately():
    """重问不是逼问：用户在任何一轮点拒绝/取消都立刻结束。"""
    mgr, _ = _mgr()
    queue = asyncio.Queue()
    cb = make_elicitation_callback(SERVER, **_ctx(mgr, queue))
    task = asyncio.create_task(cb(None, T.ElicitRequestFormParams(
        message="m", requestedSchema=FORM_SCHEMA)))

    first = await asyncio.wait_for(queue.get(), timeout=2)
    mgr.resolve(first["confirm_id"], True, action="accept", content={"name": 1})
    second = await asyncio.wait_for(queue.get(), timeout=2)
    mgr.resolve(second["confirm_id"], False, action="decline")

    result = await asyncio.wait_for(task, timeout=2)
    assert result.action == "decline"
    assert queue.empty(), "放弃是用户的选择,不该再补一条 mcp_warning"


@pytest.mark.asyncio
async def test_url_request_emits_host_punycode_and_insecure_flags():
    mgr, _ = _mgr()
    queue = asyncio.Queue()
    cb = make_elicitation_callback(SERVER, **_ctx(mgr, queue))
    task = asyncio.create_task(cb(None, T.ElicitRequestURLParams(
        message="Authorize", url="http://xn--80ak6aa92e.example.com/oauth?x=1")))
    card = await asyncio.wait_for(queue.get(), timeout=2)

    assert card["kind"] == "mcp_elicit_url"
    assert card["url"] == "http://xn--80ak6aa92e.example.com/oauth?x=1"
    assert card["host"] == "xn--80ak6aa92e.example.com"
    assert card["punycode"] is True     # 可能是品牌域名的同形异义伪装
    assert card["insecure"] is True     # 不是 https

    mgr.resolve(card["confirm_id"], True, action="accept")
    result = await asyncio.wait_for(task, timeout=2)
    # URL 模式的 accept 只代表"用户同意去打开这个链接",没有 content 可带
    assert result.action == "accept" and result.content is None


@pytest.mark.asyncio
async def test_https_ascii_host_raises_neither_flag():
    mgr, _ = _mgr()
    queue = asyncio.Queue()
    cb = make_elicitation_callback(SERVER, **_ctx(mgr, queue))
    task = asyncio.create_task(cb(None, T.ElicitRequestURLParams(
        message="Authorize", url="https://accounts.example.com/oauth")))
    card = await asyncio.wait_for(queue.get(), timeout=2)
    assert (card["punycode"], card["insecure"]) == (False, False)
    mgr.resolve(card["confirm_id"], False, action="cancel")
    await asyncio.wait_for(task, timeout=2)


# ── 真实 SDK 对象上的端到端 ────────────────────────────────────────────────────

def _build_server(script):
    """script: 每次 tools/call 依次返回的东西。on_list_tools 是必须的 —— 见模块 docstring。"""
    calls = {"n": 0, "states": []}

    async def _list(ctx, params):
        return mtypes.ListToolsResult(tools=[mtypes.Tool(
            name="greet", description="d",
            inputSchema={"type": "object", "properties": {}})])

    async def _call(ctx, params):
        calls["states"].append(getattr(params, "request_state", None))
        item = script[min(calls["n"], len(script) - 1)]
        calls["n"] += 1
        return item

    return Server("elicit-test", on_list_tools=_list, on_call_tool=_call), calls


@pytest.mark.asyncio
async def test_end_to_end_form_round_trip_through_our_own_callback():
    """一次真实的 MRTR 往返：服务端要人填名字 -> 我们的回调弹卡 -> 用户作答 ->
    ElicitResult 回到服务端 -> 服务端给出终态结果。全程真 JSON-RPC 编解码。"""
    mgr, _ = _mgr()
    queue = asyncio.Queue()
    server, calls = _build_server([
        T.InputRequiredResult(
            inputRequests={"q1": T.ElicitRequest(
                params=T.ElicitRequestFormParams(message="Who?",
                                                 requestedSchema=FORM_SCHEMA))},
            requestState="OPAQUE-42"),
        mtypes.CallToolResult(content=[mtypes.TextContent(type="text", text="hi Nimo")]),
    ])

    async def answer_the_card():
        card = await asyncio.wait_for(queue.get(), timeout=5)
        assert card["kind"] == "mcp_elicit_form"
        mgr.resolve(card["confirm_id"], True, action="accept",
                    content={"name": "Nimo"})

    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(Client(
            InMemoryTransport(server, raise_exceptions=True), mode="auto",
            read_timeout_seconds=5, cache=None,
            input_required_max_rounds=mc.MCP_INPUT_REQUIRED_ROUNDS,
            elicitation_callback=make_elicitation_callback(SERVER, **_ctx(mgr, queue))))
        result, _ = await asyncio.gather(client.call_tool("greet", {}),
                                         answer_the_card())

    assert result.content[0].text == "hi Nimo"
    # requestState 由 SDK 原样回显,我们从不碰它（_input_required.py:66
    # "passed through byte-exact and never inspected"）。
    assert calls["states"] == [None, "OPAQUE-42"]


@pytest.mark.asyncio
async def test_end_to_end_declining_does_not_kill_the_call_with_an_error():
    """decline 是一个合法答复,不是异常。服务端拿到 decline 后仍然可以正常收尾。"""
    mgr, _ = _mgr()
    queue = asyncio.Queue()
    server, _calls = _build_server([
        T.InputRequiredResult(
            inputRequests={"q1": T.ElicitRequest(
                params=T.ElicitRequestFormParams(message="Who?",
                                                 requestedSchema=FORM_SCHEMA))},
            requestState="S"),
        mtypes.CallToolResult(
            content=[mtypes.TextContent(type="text", text="ok, skipped")]),
    ])

    async def refuse():
        card = await asyncio.wait_for(queue.get(), timeout=5)
        mgr.resolve(card["confirm_id"], False, action="decline")

    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(Client(
            InMemoryTransport(server, raise_exceptions=True), mode="auto",
            read_timeout_seconds=5, cache=None,
            input_required_max_rounds=mc.MCP_INPUT_REQUIRED_ROUNDS,
            elicitation_callback=make_elicitation_callback(SERVER, **_ctx(mgr, queue))))
        result, _ = await asyncio.gather(client.call_tool("greet", {}), refuse())

    assert result.content[0].text == "ok, skipped"


@pytest.mark.asyncio
async def test_state_only_polling_eventually_exhausts_the_round_cap():
    """只带 requestState 的响应也算一轮,而且不问用户。这就是 URL 模式下真实 OAuth
    服务端的预期落点 —— 第二期自觉接受这个结果,并靠 _rounds_exceeded_msg 给出
    "去完成授权再重试"而不是"去改服务器配置"。"""
    from mcp.client import InputRequiredRoundsExceededError

    mgr, _ = _mgr()
    queue = asyncio.Queue()
    server, _calls = _build_server([T.InputRequiredResult(requestState="STILL-WAITING")])

    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(Client(
            InMemoryTransport(server, raise_exceptions=True), mode="auto",
            read_timeout_seconds=5, cache=None,
            input_required_max_rounds=mc.MCP_INPUT_REQUIRED_ROUNDS,
            elicitation_callback=make_elicitation_callback(SERVER, **_ctx(mgr, queue))))
        with pytest.raises(InputRequiredRoundsExceededError):
            await client.call_tool("greet", {})

    assert queue.empty(), "state-only 轮次不该弹任何卡"


@pytest.mark.asyncio
async def test_production_connect_installs_the_callback():
    """钉住接线本身：不是"能写出一个回调",而是 _connect 真的把它交给了 Client。"""
    captured = {}
    real_client = mc.Client

    class _Spy:
        def __init__(self, *a, **kw):
            captured.update(kw)
            self._inner = real_client(*a, **kw)

        async def __aenter__(self):
            return await self._inner.__aenter__()

        async def __aexit__(self, *a):
            return await self._inner.__aexit__(*a)

    server, _ = _build_server([mtypes.CallToolResult(content=[])])
    transport = InMemoryTransport(server, raise_exceptions=True)

    async def _fake_transport(srv, stack, connect_to, session_to):
        return transport

    orig_bt, orig_cl = mc._build_transport, mc.Client
    mc._build_transport, mc.Client = _fake_transport, _Spy
    try:
        conn = await mc._connect({"id": 1, "name": "notion", "transport": "http",
                                  "url": "http://x"})
        await conn.aclose()
    finally:
        mc._build_transport, mc.Client = orig_bt, orig_cl

    assert captured.get("elicitation_callback") is not None
    assert captured.get("input_required_max_rounds") == mc.MCP_INPUT_REQUIRED_ROUNDS
    # 第二期仍然不声明这两个 —— 见 Global Constraints 第 9 条
    assert "sampling_callback" not in captured
    assert "list_roots_callback" not in captured
