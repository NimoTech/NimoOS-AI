"""渐进式工具暴露的运行时门控原语。

解锁集放 ContextVar(与 agent.py 其它 *_VAR 一致的并发隔离模式):每个 run
在开始时 UNLOCKED_VAR.set(<从会话载入的集合>),expand_tools 原地 update 它,
非常驻工具的 is_enabled 回调读它决定是否对模型可见。
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Callable

# run 开始时由 agent.py 注入;默认空集兜底(测试/异常路径)。
UNLOCKED_VAR: ContextVar[set[str]] = ContextVar("nimoos_unlocked_categories")
GATING_SESSION_VAR: ContextVar[str] = ContextVar("nimoos_gating_session")


def current_unlocked() -> set[str]:
    return UNLOCKED_VAR.get(set())


def make_is_enabled(category: str) -> Callable[[Any, Any], bool]:
    """生成 SDK is_enabled 回调:该类别在已解锁集合中才可见。

    SDK 以 (RunContextWrapper, AgentBase) 调用;此处忽略二者,直接读 ContextVar
    (本仓库的隔离机制),返回 bool(同步即可,SDK 接受 MaybeAwaitable[bool])。
    """
    def _is_enabled(_ctx: Any, _agent: Any) -> bool:
        return category in current_unlocked()
    return _is_enabled
