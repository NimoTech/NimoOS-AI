"""Single source of truth for tool categorization: the always-on set + 8 gated categories.

Progressive tool exposure uses this to split the general profile's tools into
categories. CORE is always visible; the rest are unlocked within a session via
expand_tools, by category. MCP runtime tools are not in this static table —
they're dynamically filed under the 'mcp' category when agent.py assembles tools.
"""
from __future__ import annotations

from skills.app_management import ALL_TOOLS as APP_TOOLS
from skills.storage import ALL_TOOLS as STORAGE_TOOLS
from skills.healthcheck import ALL_TOOLS as HEALTHCHECK_TOOLS
from skills.message_bus import ALL_TOOLS as MESSAGEBUS_TOOLS
from skills.filesystem import ALL_TOOLS as FS_TOOLS
from skills.photos import ALL_TOOLS as PHOTOS_TOOLS
from skills.mcp_admin import ALL_TOOLS as MCP_ADMIN_TOOLS
from skills.tasks_admin import ALL_TOOLS as TASKS_ADMIN_TOOLS
from skills.toolbox_admin import ALL_TOOLS as TOOLBOX_TOOLS
from skills.search import SEARCH_TOOLS
from skills import WIKI_TOOLS
from skills.notes import NOTES_TOOLS
from skills.web import WEB_TOOLS

CORE_TOOL_NAMES: frozenset[str] = frozenset({
    "run_command", "read_file", "list_dir",
    "nimoos_search", "read_skill_file",
    "remember", "forget", "recall",
})


def _name(tool) -> str:
    return getattr(tool, "name", getattr(tool, "__name__", ""))


def _exclude(tools, names: set[str]) -> list:
    return [t for t in tools if _name(t) not in names]


# Carve the gated members out of each module (excluding tools already promoted to always-on).
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
    "web": list(WEB_TOOLS),                # 2
    "mcp": list(MCP_ADMIN_TOOLS),          # 1 (+ dynamic runtime MCP tools)
    "toolbox": list(TOOLBOX_TOOLS),         # 1
    "tasks": list(TASKS_ADMIN_TOOLS),       # 2
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
    "web": "search the public web and fetch web pages (needs a configured search provider)",
    "mcp": "register and use tools from connected MCP servers",
    "toolbox": "Install/manage persistent CLI components (toolbox)",
    "tasks": "create scheduled agent tasks (created disabled; the user "
             "authorizes & enables them on the Tasks page); inside a task "
             "continuation run, revise that task's own prompt",
}

# tool name → category name (returns None for always-on/unknown)
_NAME_TO_CATEGORY: dict[str, str] = {
    _name(t): cat for cat, tools in CATEGORY_TOOLS.items() for t in tools
}


def category_of(tool_name: str) -> str | None:
    return _NAME_TO_CATEGORY.get(tool_name)
