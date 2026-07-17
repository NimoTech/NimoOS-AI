"""工具分类的单一事实来源:常驻集 + 8 个门控类别。

渐进式工具暴露用它把 general profile 的工具按类别划分。CORE 永远可见;
其余按类别经 expand_tools 在会话内解锁。MCP 运行时工具不在此静态表中——
它们在 agent.py 组装时动态归入 'mcp' 类。
"""
from __future__ import annotations

from skills.app_management import ALL_TOOLS as APP_TOOLS
from skills.storage import ALL_TOOLS as STORAGE_TOOLS
from skills.healthcheck import ALL_TOOLS as HEALTHCHECK_TOOLS
from skills.message_bus import ALL_TOOLS as MESSAGEBUS_TOOLS
from skills.filesystem import ALL_TOOLS as FS_TOOLS
from skills.photos import ALL_TOOLS as PHOTOS_TOOLS
from skills.mcp_admin import ALL_TOOLS as MCP_ADMIN_TOOLS
from skills.search import SEARCH_TOOLS
from skills import WIKI_TOOLS
from skills.notes import NOTES_TOOLS

CORE_TOOL_NAMES: frozenset[str] = frozenset({
    "run_command", "read_file", "list_dir",
    "nimoos_search", "read_skill_file",
    "remember", "forget", "recall",
})


def _name(tool) -> str:
    return getattr(tool, "name", getattr(tool, "__name__", ""))


def _exclude(tools, names: set[str]) -> list:
    return [t for t in tools if _name(t) not in names]


# 从各模块切出门控成员(剔除已提升为常驻的工具)。
_FILES = _exclude(FS_TOOLS, {"read_file", "list_dir"})           # 9
_DOCUMENTS = _exclude(SEARCH_TOOLS, {"nimoos_search"})           # 3
_SYSTEM = list(HEALTHCHECK_TOOLS) + list(STORAGE_TOOLS)         # 3 + 2 = 5

CATEGORY_TOOLS: dict[str, list] = {
    "apps": list(APP_TOOLS),               # 10
    "files": _FILES,                       # 9
    "photos": list(PHOTOS_TOOLS),          # 7
    "wiki": list(WIKI_TOOLS),              # 6
    "documents": _DOCUMENTS,              # 3
    "system": _SYSTEM,                     # 5
    "events": list(MESSAGEBUS_TOOLS),      # 3
    "notes": list(NOTES_TOOLS),
    "mcp": list(MCP_ADMIN_TOOLS),          # 1(+ 运行时动态 MCP 工具)
}

CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "apps": "install/search/start/stop/restart/uninstall/update Docker apps, view logs",
    "files": "write/edit/delete/mkdir/rename/batch ops/glob/full-text file search",
    "photos": "semantic photo search, album management, view images",
    "wiki": "knowledge-base node read/write, user notes, register roots",
    "documents": "chunked document reading, docling parsing, page views",
    "system": "service/port health, system logs, disks and mounts",
    "events": "message-bus event types, action types, trigger actions",
    "notes": "knowledge notes: save/update/consult long-term conclusions and summaries (writes require user confirmation)",
    "mcp": "register and use tools from connected MCP servers",
}

# 工具名 → 类别名(常驻/未知返回 None)
_NAME_TO_CATEGORY: dict[str, str] = {
    _name(t): cat for cat, tools in CATEGORY_TOOLS.items() for t in tools
}


def category_of(tool_name: str) -> str | None:
    return _NAME_TO_CATEGORY.get(tool_name)
