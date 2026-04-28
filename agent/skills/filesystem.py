"""Filesystem tool surface for the agent loop.

Each tool is a 1-liner around fs.ops.* that pulls the per-run context out of
ContextVars set by agent.py before every agent run.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

from agents import function_tool

from fs import ops as fsops


# ContextVars set by agent.py::run() before every agent loop start.
SESSION_ID_VAR: ContextVar[str] = ContextVar("session_id")
RUN_ID_VAR: ContextVar[str] = ContextVar("run_id")
EVENT_QUEUE_VAR: ContextVar = ContextVar("event_queue")
DB_VAR: ContextVar = ContextVar("db")
STORE_VAR: ContextVar = ContextVar("store")
CHAT_USERNAME_VAR: ContextVar[str] = ContextVar("chat_username")
USER_PATTERNS_VAR: ContextVar[list] = ContextVar("user_patterns")


def _ctx() -> dict:
    return {
        "session_id": SESSION_ID_VAR.get(),
        "run_id": RUN_ID_VAR.get(),
        "sink": EVENT_QUEUE_VAR.get(),
        "conn": DB_VAR.get(),
        "store": STORE_VAR.get(),
        "chat_username": CHAT_USERNAME_VAR.get(""),
        "user_patterns": USER_PATTERNS_VAR.get([]),
    }


@function_tool
async def list_dir(path: str) -> str:
    """List directory entries (files+subdirs) inside the agent's visible scope."""
    return await fsops.list_dir(_ctx(), path)


@function_tool
async def read_file(path: str) -> str:
    """Read a UTF-8 text file. Refuses binary; refuses files >1 MiB."""
    return await fsops.read_file(_ctx(), path)


@function_tool
async def read_file_lines(path: str, start: int, end: int) -> str:
    """Read lines [start, end) from a file (1-indexed; max 2000 lines)."""
    return await fsops.read_file_lines(_ctx(), path, start, end)


@function_tool
async def write_file(path: str, content: str) -> str:
    """Create or overwrite a text file. The change enters a staging area
    for user review."""
    return await fsops.write_file(_ctx(), path, content)


@function_tool
async def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Replace `old_string` with `new_string` in a file. The match must be
    unique; add more surrounding context if it isn't. Enters staging."""
    return await fsops.edit_file(_ctx(), path, old_string, new_string)


@function_tool
async def delete_path(path: str, recursive: bool = False) -> str:
    """Delete a file or empty directory. For non-empty directories pass
    recursive=True. Enters staging."""
    return await fsops.delete_path(_ctx(), path, recursive)


@function_tool
async def mkdir(path: str, parents: bool = False) -> str:
    """Create a directory. Enters staging."""
    return await fsops.mkdir(_ctx(), path, parents)


@function_tool
async def rename(src: str, dst: str) -> str:
    """Rename or move a file/dir. dst must NOT already exist (delete it
    first if you want to replace). Enters staging."""
    return await fsops.rename(_ctx(), src, dst)


@function_tool
async def glob_files(pattern: str, root: str) -> str:
    """Find files matching a glob pattern (e.g. '**/*.py') under root."""
    return await fsops.glob_files(_ctx(), pattern, root)


@function_tool
async def search_content(query: str, root: str,
                          glob_pattern: Optional[str] = None) -> str:
    """Search file contents with a regex under root, optionally filtered by
    a filename glob. Returns up to 100 file:line:text matches."""
    return await fsops.search_content(_ctx(), query, root, glob_pattern)


ALL_TOOLS = [
    list_dir, read_file, read_file_lines,
    write_file, edit_file, delete_path, mkdir, rename,
    glob_files, search_content,
]
