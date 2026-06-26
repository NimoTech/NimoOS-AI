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


# ---------------------------------------------------------------------------
# expand_tools 元工具
# ---------------------------------------------------------------------------

from agents import function_tool
from skills import tool_registry as _reg


def _name(tool) -> str:
    return getattr(tool, "name", getattr(tool, "__name__", ""))


def categories_overview() -> str:
    lines = ["可解锁的工具类别(调用 expand_tools 解锁后,工具会在下一步出现):"]
    for cat, desc in _reg.CATEGORY_DESCRIPTIONS.items():
        lines.append(f"- {cat}: {desc}")
    return "\n".join(lines)


def _persist(categories: list[str]) -> None:
    """把当前解锁集落库(独立函数,便于测试 monkeypatch)。"""
    import db
    session_id = GATING_SESSION_VAR.get("")
    if session_id:
        db.set_unlocked_categories(session_id, sorted(current_unlocked()))


def expand_categories(categories: list[str]) -> str:
    """纯逻辑:解锁给定类别,返回给模型看的文本。被 expand_tools 包裹。"""
    if not categories:
        return categories_overview()
    valid = set(_reg.CATEGORY_TOOLS.keys())
    unknown = [c for c in categories if c not in valid]
    if unknown:
        return (f"未知类别 {unknown}。合法类别:" + ", ".join(sorted(valid)) +
                "。请用这些类别名重试。")
    cur = current_unlocked()
    if not isinstance(cur, set):     # 兜底:确保可原地修改
        cur = set(cur)
        UNLOCKED_VAR.set(cur)
    newly = [c for c in categories if c not in cur]
    cur.update(categories)
    _persist(sorted(cur))
    lines = [f"已解锁:{', '.join(categories)}。现在可用以下工具:"]
    for c in categories:
        for t in _reg.CATEGORY_TOOLS[c]:
            lines.append(f"- {_name(t)}")
    if not newly:
        lines.append("(这些类别此前已解锁)")
    return "\n".join(lines)


@function_tool
def expand_tools(categories: list[str]) -> str:
    """解锁一组工具类别,使其工具在下一步可被调用。

    起步时你只有少量核心工具。需要其他能力时,先用本工具解锁相应类别,
    一次可传多个,尽量一次性解锁本任务预计要用的所有类别。
    传空列表则返回所有可解锁类别的总览。

    Args:
        categories: 要解锁的类别名列表,如 ["apps", "files"]。
    """
    return expand_categories(categories)
