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
from skills.memory import MEMORY_TOOLS
from skills import WIKI_TOOLS

CORE_TOOL_NAMES: frozenset[str] = frozenset({
    "run_command", "read_file", "list_dir",
    "nimoos_search", "list_skills", "read_skill_file",
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
    "memory": list(MEMORY_TOOLS),          # 2
    "mcp": list(MCP_ADMIN_TOOLS),          # 1(+ 运行时动态 MCP 工具)
}

CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "apps": "安装/搜索/启停/重启/卸载/更新/查看日志 Docker 应用",
    "files": "写/改/删/建目录/重命名/批量/glob/全文搜索文件",
    "photos": "照片语义搜索、相册管理、看图",
    "wiki": "知识库节点读写、用户笔记、登记根目录",
    "documents": "文档分块读取、docling 解析、翻页",
    "system": "服务/端口健康、系统日志、磁盘与挂载",
    "events": "消息总线事件类型、动作类型、触发动作",
    "memory": "记住/查询/遗忘用户的跨会话记忆偏好与事实",
    "mcp": "注册并使用已连接的 MCP 服务工具",
}

# 工具名 → 类别名(常驻/未知返回 None)
_NAME_TO_CATEGORY: dict[str, str] = {
    _name(t): cat for cat, tools in CATEGORY_TOOLS.items() for t in tools
}


def category_of(tool_name: str) -> str | None:
    return _NAME_TO_CATEGORY.get(tool_name)
