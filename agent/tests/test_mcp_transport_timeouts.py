"""传输层的空闲超时契约。

elicitation 期间链路上没有任何 in-flight 请求 —— 服务端已经用一个完整的
InputRequiredResult 答复过了,我们在自己的回调里等用户。于是唯一还在跑的钟就是
传输自己的空闲读超时。这个文件把那些值钉住,免得它们悄悄回落到 SDK 默认值。
"""
import inspect
from contextlib import AsyncExitStack

import pytest
from mcp.client.sse import sse_client

import mcp_client.client as mc


def test_sse_client_still_has_the_parameter_we_rely_on():
    """SDK 升级把 sse_read_timeout 改名/删掉的话,这里先红,而不是在生产里
    静默回落到 300 秒。"""
    params = inspect.signature(sse_client).parameters
    assert "sse_read_timeout" in params
    assert params["sse_read_timeout"].default == 300.0, (
        "SDK 默认值变了 —— 重新评估 MCP_SSE_READ_TIMEOUT 的注释是否还成立")


def test_the_sse_idle_timeout_covers_a_url_authorization_wait():
    """URL 授权卡最长等 URL_ELICIT_WAIT,这段时间里 sse 链路必须不被自己掐掉。"""
    from mcp_client.elicitation import URL_ELICIT_WAIT
    assert mc.MCP_SSE_READ_TIMEOUT > URL_ELICIT_WAIT


@pytest.mark.asyncio
async def test_the_sse_branch_passes_our_idle_timeout_not_the_sdk_default(monkeypatch):
    seen = {}

    def fake_sse_client(url, headers=None, timeout=None, sse_read_timeout=None):
        seen.update(url=url, timeout=timeout, sse_read_timeout=sse_read_timeout)
        return "transport-sentinel"

    monkeypatch.setattr(mc, "sse_client", fake_sse_client)
    async with AsyncExitStack() as stack:
        got = await mc._build_transport(
            {"transport": "sse", "url": "https://s.example/sse"}, stack,
            connect_to=8, session_to=mc.MCP_SESSION_TIMEOUT)

    assert got == "transport-sentinel"
    assert seen["timeout"] == mc.MCP_SESSION_TIMEOUT
    assert seen["sse_read_timeout"] == mc.MCP_SSE_READ_TIMEOUT


# ── http 传输:长空闲之后 MRTR 重试还能不能落地 ────────────────────────────────

def _run_streamable_http_server_in_a_thread(sock, holder):
    """在**独立线程的独立事件循环**里跑完整个服务端(manager.run() + uvicorn)。

    为什么必须隔到线程里:把 `uvicorn.Server.serve()` 用 `tg.start_soon` 塞进测试
    自己持有的 `anyio.create_task_group()`,同时又在同一个任务里进出
    `StreamableHTTPSessionManager.run()`(它内部还有自己的任务组),会在退出时把
    anyio 的 cancel-scope 记账搞乱,teardown 必炸
    `RuntimeError: Attempted to exit a cancel scope that isn't the current
    tasks's current cancel scope`(实测:哪怕只做一次 `_connect` 就停手也炸,
    跟空闲时长/超时值/重连次数无关)。放到独立线程后,测试协程里只剩客户端侧的
    工作,没有任何外来任务组,teardown 干净。

    端口由测试自己 bind 好再交给 uvicorn(`serve(sockets=[sock])`),所以不需要
    用 port=0 再轮询猜端口。
    """
    import asyncio

    import mcp.types as mtypes
    import uvicorn
    from mcp.server.lowlevel import Server
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.shared.exceptions import MCPError

    async def _list(ctx, params):
        return mtypes.ListToolsResult(tools=[mtypes.Tool(
            name="ping", description="ping",
            inputSchema={"type": "object", "properties": {}})])

    async def _call(ctx, params):
        return mtypes.CallToolResult(
            content=[mtypes.TextContent(type="text", text="pong")])

    async def _no_discover(ctx, params):
        # METHOD_NOT_FOUND,跟一个真实的 2026 之前的老服务端一样。
        raise MCPError(-32601, "Method not found: server/discover")

    async def main():
        srv = Server("idle-probe", on_list_tools=_list, on_call_tool=_call)
        # 拒掉 server/discover,把客户端逼回旧的 initialize 握手 —— 这是走到被测
        # 机制的唯一办法,理由见测试函数的 docstring。
        srv.add_request_handler("server/discover", mtypes.RequestParams,
                                _no_discover)
        manager = StreamableHTTPSessionManager(app=srv, json_response=True,
                                               stateless=False)

        async def asgi(scope, receive, send):
            await manager.handle_request(scope, receive, send)

        server = uvicorn.Server(uvicorn.Config(asgi, log_level="error",
                                               lifespan="off"))
        holder["server"] = server
        async with manager.run():
            await server.serve(sockets=[sock])

    asyncio.run(main())


@pytest.mark.asyncio
async def test_an_idle_http_session_can_still_complete_a_tool_call(caplog):
    """URL 授权卡会让链路空闲几分钟。这条测试把 httpx 的超时压到 2 秒、空闲 8 秒,
    用 1/30 的尺度复现同一个机制:服务端→客户端的 GET listen 流被读超时打断、重连
    2 次后被彻底放弃(streamable_http.py::MAX_RECONNECTION_ATTEMPTS = 2),此后那次
    工具调用还能不能成。

    这不是在测 SDK,是在测我们把一个 60 秒超时的 httpx client 整个交给它的这个决定
    (client.py 的 http 分支)。

    时钟算术(读超时压到 2 秒后):
      t+0    GET 流建立
      t+2    读超时 → attempt=1 → 等 DEFAULT_RECONNECTION_DELAY_MS = 1000ms
      t+3    GET 流重连建立
      t+5    读超时 → attempt=2 == MAX_RECONNECTION_ATTEMPTS → 彻底放弃
    5 秒就走完两次尝试,所以 8 秒的空闲足够穿过去,而且留了余量。

    服务端拒掉 `server/discover` 不是为了方便,是为了让这条测试测到东西:
    `start_get_stream()` 只在客户端发出 `notifications/initialized` 时被调用
    (streamable_http.py:566),而那条通知只走**旧的 initialize 握手**。新的
    `server/discover` 握手根本不发它 —— 也就是说对现代服务端而言这条 GET 流压根
    不存在,这个机制无从触发。我们的 `_connect` 用 mode="auto"(先探 discover,失败
    才回落 initialize),所以只有面对老服务端时才会开这条 GET 流。让测试服务端拒掉
    discover,才能真正走到被测的那条路上。下面的日志断言就是防止这条测试哪天悄悄
    变成"什么都没测到还是绿的"。
    """
    import logging
    import socket
    import threading

    import anyio

    # _session_timeout() 在调用时读模块级常量(client.py:98),所以直接改模块属性
    # 就同时压低了 httpx client 的 timeout 和 read_timeout_seconds。本地服务端的
    # list_tools / call_tool 都是毫秒级,2 秒对它们绰绰有余。
    #
    # 这 2 秒同时覆盖 connect **和整个 legacy initialize 握手**,不只是读超时。所以
    # 这条测试在高负载机器上变红说明的是**机器负载**,不是被测机制失效 —— 别用"把
    # 超时调大"来修:上面那段 5s-vs-8s 的算术就是靠这个值成立的,调大它会让 8 秒空闲
    # 走不完两次重连尝试,测试于此静默退化成"什么都没测到还是绿的"。真要动,先把整段
    # 算术连同空闲时长一起重算。
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mc, "MCP_SESSION_TIMEOUT", 2)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(64)
        port = sock.getsockname()[1]

        holder: dict = {}
        thread = threading.Thread(
            target=_run_streamable_http_server_in_a_thread,
            args=(sock, holder), daemon=True)
        thread.start()
        try:
            for _ in range(200):
                server = holder.get("server")
                if server is not None and server.started:
                    break
                await anyio.sleep(0.05)
            assert holder.get("server") is not None and holder["server"].started, \
                "uvicorn 没在线程里起来"

            with caplog.at_level(logging.DEBUG,
                                 logger="mcp.client.streamable_http"):
                conn = await mc._connect({"id": 99, "name": "idle-probe",
                                          "transport": "http",
                                          "url": f"http://127.0.0.1:{port}/mcp"})
                try:
                    await conn.list_tools()
                    await anyio.sleep(8)          # 空闲穿过 2 次重连尝试
                    result = await conn.call_tool("ping", {})
                    # NOTE: CallToolResult 只有 snake_case 的 is_error;wire 上是
                    # isError,Python 属性上没有 —— 跟 client.py:199-210 记录的
                    # input_schema/inputSchema 是同一类陷阱。
                    assert not result.is_error
                    assert result.content[0].text == "pong"
                finally:
                    await conn.aclose()

            # 证明这条测试真的走到了被测机制上,而不是碰巧绿的:
            log = "\n".join(r.getMessage() for r in caplog.records)
            assert "GET SSE connection established" in log, \
                f"GET listen 流压根没开起来,这条测试什么都没测到:\n{log}"
            assert "max reconnection attempts" in log, \
                f"GET 流没有被放弃 —— 空闲时长或超时值不再匹配这个机制:\n{log}"
        finally:
            server = holder.get("server")
            if server is not None:
                server.should_exit = True
            thread.join(timeout=15)
            sock.close()

        # 放在 finally 外面:body 已经失败时不要用这条断言把真正的错因挡掉。
        assert not thread.is_alive(), "uvicorn 线程没退干净,端口会被占住"
