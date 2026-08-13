"""Runtime gating primitives for progressive tool exposure.

The unlocked set lives in a ContextVar (same concurrency-isolation pattern as
the other *_VAR globals in agent.py): each run does UNLOCKED_VAR.set(<set
loaded from the session>) at the start, expand_tools updates it in place, and
the is_enabled callback of non-always-on tools reads it to decide whether
they're visible to the model.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Callable

# Injected by agent.py at the start of a run; defaults to an empty set as a
# fallback (tests/error paths).
UNLOCKED_VAR: ContextVar[set[str]] = ContextVar("nimoos_unlocked_categories")
GATING_SESSION_VAR: ContextVar[str] = ContextVar("nimoos_gating_session")


def current_unlocked() -> set[str]:
    return UNLOCKED_VAR.get(set())


def make_is_enabled(category: str) -> Callable[[Any, Any], bool]:
    """Build an SDK is_enabled callback: visible only once this category is unlocked.

    The SDK calls it as (RunContextWrapper, AgentBase); both are ignored here —
    we read the ContextVar directly (this repo's isolation mechanism) and
    return a bool (sync is fine, the SDK accepts MaybeAwaitable[bool]).
    """
    def _is_enabled(_ctx: Any, _agent: Any) -> bool:
        return category in current_unlocked()
    return _is_enabled


# ---------------------------------------------------------------------------
# expand_tools meta-tool
# ---------------------------------------------------------------------------

from agents import function_tool
from skills import tool_registry as _reg


def _name(tool) -> str:
    return getattr(tool, "name", getattr(tool, "__name__", ""))


def categories_overview() -> str:
    lines = ["Unlockable tool categories (call expand_tools; unlocked tools appear on the next step):"]
    for cat, desc in _reg.CATEGORY_DESCRIPTIONS.items():
        lines.append(f"- {cat}: {desc}")
    return "\n".join(lines)


def _persist(categories: list[str]) -> None:
    """Persist the given list of unlocked categories (a standalone function so tests can monkeypatch it)."""
    import db
    session_id = GATING_SESSION_VAR.get("")
    if session_id:
        db.set_unlocked_categories(session_id, categories)


def _mcp_runtime_lines() -> list[str]:
    """Render the per-run MCP snapshot (same data as the system-prompt status
    line, at action-level detail). The static CATEGORY_TOOLS table only knows
    add_mcp_server — rendering it alone told the model no servers were
    connected (defect 2). On ANY failure fall back to a line that promises
    nothing rather than one that lies."""
    try:
        from mcp_client import status as _st
        return _st.render_expand_section(_st.MCP_STATUS_VAR.get())
    except Exception:
        return ["MCP runtime tools, if any, appear in your tool list on the next step."]


def expand_categories(categories: list[str]) -> str:
    """Pure logic: unlock the given categories, return the text shown to the model. Wrapped by expand_tools."""
    if not categories:
        return categories_overview()
    valid = set(_reg.CATEGORY_TOOLS.keys())
    unknown = [c for c in categories if c not in valid]
    if unknown:
        return (f"Unknown categories {unknown}. Valid categories: " + ", ".join(sorted(valid)) +
                ". Retry with these category names.")
    cur = current_unlocked()
    if not isinstance(cur, set):     # fallback: ensure it can be mutated in place
        cur = set(cur)
        UNLOCKED_VAR.set(cur)
    newly = [c for c in categories if c not in cur]
    cur.update(categories)
    _persist(sorted(cur))
    lines = [f"Unlocked: {', '.join(categories)}. The following tools are now available:"]
    for c in categories:
        for t in _reg.CATEGORY_TOOLS[c]:
            if c == "mcp":
                # label the admin tool and terminate with ";" so it reads as
                # one line among peers, not as the sole tool that "owns" the
                # server tool lists rendered below (doubao misread that layout
                # and kept calling the register tool instead of mcp__* tools)
                lines.append(f"System tool: {_name(t)};")
            else:
                lines.append(f"- {_name(t)}")
        if c == "mcp":
            lines.extend(_mcp_runtime_lines())
    if not newly:
        lines.append("(these categories were already unlocked — every tool named "
                      "above, with its full description, is ALREADY in your tool "
                      "list; call it directly by that exact name)")
    return "\n".join(lines)


@function_tool
def expand_tools(categories: list[str]) -> str:
    """Unlock a set of tool categories, making their tools callable on the next step.

    At the start you only have a small set of core tools. When you need other
    capabilities, use this tool first to unlock the relevant categories — you
    can pass several at once, and should try to unlock all the categories this
    task is expected to need in one call.
    Passing an empty list returns an overview of all unlockable categories.

    Args:
        categories: List of category names to unlock, e.g. ["apps", "files"].
    """
    return expand_categories(categories)
