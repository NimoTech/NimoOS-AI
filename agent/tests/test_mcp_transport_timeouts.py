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
