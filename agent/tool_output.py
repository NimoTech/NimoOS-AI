"""Tool-output offload (spec §4): oversized tool results leave the conversation.

Why: a scheduled task made 106 tool calls in one run; 46 web_fetch bodies of
20-30 KB each put 317k tokens in front of the model and it degraded (empty
tool arguments, garbled text). Compaction cannot help while the bulk is in the
LAST turns. So every tool result above OFFLOAD_THRESHOLD_CHARS is written to a
file and replaced by a short fenced preview plus one machine-parsable trailer
line that names the file. The model pages through it with read_file_lines /
search_content; the UI shows "folded · N chars · view".

Where the files live is a CONSTRAINT: /var/lib/nimoos is in FS_DENY_ROOTS and
the fs hard blacklist, so a folder there would be granted then silently
refused (same lesson as tasks/workspace.py). Task sessions use the task's own
workspace (already granted); chat sessions use ROOT/<session_id>, which
fs/paths.resolve allows implicitly.

Nothing here raises into a tool call. Any failure → the original output is
returned unchanged (never lose data to a treatment step).
"""
from __future__ import annotations

import dataclasses
import logging
import os
import re
import time
from contextvars import ContextVar

from fences import fence_untrusted

_LOG = logging.getLogger("nimoos-agent.tool_output")

DEFAULT_ROOT = "/DATA/AppData/nimoos-agent/tool-outputs"
ROOT = os.environ.get("NIMOOS_TOOL_OUTPUT_ROOT", "").strip() or DEFAULT_ROOT
OFFLOAD_THRESHOLD_CHARS = int(
    os.environ.get("NIMOOS_OFFLOAD_THRESHOLD_CHARS", "").strip() or 6000)
PREVIEW_HEAD = 1500
PREVIEW_TAIL = 300
TTL_DAYS = 7
TASK_SUBDIR = ".tool-outputs"
MAX_READ_BYTES = 5 * 1024 * 1024

# Set by agent.py at run start (and by Task 2's wrapper per call).
OFFLOAD_DIR_VAR: ContextVar[str] = ContextVar("offload_dir", default="")
CALL_ID_VAR: ContextVar[str] = ContextVar("tool_call_id", default="")
# Per-run scratch dict shared by tools that want run-scoped memory (web_fetch
# dedup). agent.py sets a fresh {} per run; tools tolerate it being unset.
RUN_SCRATCH_VAR: ContextVar[dict] = ContextVar("run_scratch")

# Tools whose output is structured data the UI parses directly (not prose a
# model pages through); folding it into a placeholder would break that UI.
OFFLOAD_EXEMPT_TOOLS = frozenset({"search_photos"})

_SAFE_CALL_ID = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
# CONTRACT shared with the UI (streamMappers.ts OFFLOAD_TRAILER_RE). Byte-exact.
TRAILER_RE = re.compile(r"\[tool output offloaded: chars=(\d+) path=(\S+)\]")


def is_safe_call_id(call_id: str) -> bool:
    return bool(isinstance(call_id, str) and _SAFE_CALL_ID.match(call_id))


def chat_dir_for_session(session_id: str) -> str:
    return os.path.join(ROOT, session_id)


def resolve_offload_dir(conn, session_id: str) -> str:
    """Task session → <workspace>/.tool-outputs; anything else → ROOT/<sid>.
    Never raises; the chat folder is the fallback for every lookup failure."""
    try:
        row = conn.execute("SELECT source FROM sessions WHERE id=?",
                           (session_id,)).fetchone()
        if row is not None and row["source"] == "task":
            tr = conn.execute(
                "SELECT task_id FROM task_runs WHERE session_id=? "
                "ORDER BY created_at DESC LIMIT 1", (session_id,)).fetchone()
            if tr is not None:
                from tasks import workspace  # noqa: PLC0415 — avoid import cycle
                ws = workspace.path_for(tr["task_id"])
                if ws:
                    return os.path.join(ws, TASK_SUBDIR)
    except Exception:  # noqa: BLE001 — folder choice must never sink a run
        _LOG.debug("resolve_offload_dir failed for %s", session_id, exc_info=True)
    return chat_dir_for_session(session_id)


def ensure_offload_dir(conn, session_id: str) -> str:
    d = resolve_offload_dir(conn, session_id)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as exc:
        _LOG.warning("tool_output: cannot create %s: %s — offload disabled "
                     "for this run", d, exc)
        return ""
    return d


def store_output(text: str, *, call_id: str, tool_name: str = "") -> str:
    d = OFFLOAD_DIR_VAR.get("")
    if not d or not is_safe_call_id(call_id):
        return ""
    path = os.path.join(d, f"{call_id}.txt")
    tmp = path + ".tmp"
    try:
        os.makedirs(d, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001 — any write failure must not sink a run
        _LOG.warning("tool_output: cannot store %s output to %s: %s",
                     tool_name, path, exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return ""
    return path


def make_placeholder(text: str, *, tool_name: str, path: str, chars: int) -> str:
    head = text[:PREVIEW_HEAD]
    tail = text[-PREVIEW_TAIL:] if chars > PREVIEW_HEAD + PREVIEW_TAIL else ""
    omitted = chars - len(head) - len(tail)
    body = head
    if tail:
        body += f"\n…[{omitted} chars omitted]…\n{tail}"
    fenced = fence_untrusted("tool-output-preview", body,
                             cap=PREVIEW_HEAD + PREVIEW_TAIL + 200)
    folder = os.path.dirname(path)
    return (
        f"{fenced}\n"
        f"[tool output offloaded: chars={chars} path={path}]\n"
        f"The full {tool_name or 'tool'} output ({chars} chars) was too large for "
        f"the conversation and was saved to that file. Read it in small slices "
        f"with read_file_lines(path, start, end) — about 50-100 lines per call; "
        f"a slice longer than {OFFLOAD_THRESHOLD_CHARS} chars is folded again. "
        f"To find text inside it use search_content(query, root={folder}). If "
        f'read_file_lines/search_content are not in your tool list, call '
        f'expand_tools(["files"]) first. Do not re-run the tool just to see '
        f"more of this result."
    )


def postprocess(output, *, tool_name: str, call_id: str):
    """Replace an oversized str result with a placeholder; everything else
    (non-str outputs, small outputs, existing placeholders, store failures)
    passes through untouched."""
    if not isinstance(output, str):
        return output
    if tool_name in OFFLOAD_EXEMPT_TOOLS or output.startswith("[MCP error]"):
        return output
    n = len(output)
    if n <= OFFLOAD_THRESHOLD_CHARS:
        return output
    if TRAILER_RE.search(output):
        return output
    cid = call_id if is_safe_call_id(call_id) else f"t{int(time.time() * 1000)}"
    path = store_output(output, call_id=cid, tool_name=tool_name)
    if not path:
        return output
    return make_placeholder(output, tool_name=tool_name, path=path, chars=n)


def _within(child: str, parent: str) -> bool:
    parent = parent.rstrip(os.sep)
    return child == parent or child.startswith(parent + os.sep)


def is_offload_path(abs_path: str) -> bool:
    """True for anything under ROOT, or under <task workspace>/.tool-outputs."""
    real = os.path.realpath(abs_path)
    if _within(real, os.path.realpath(ROOT)):
        return True
    try:
        from tasks import workspace  # noqa: PLC0415
        troot = os.path.realpath(workspace.ROOT)
    except Exception:  # noqa: BLE001
        return False
    if not _within(real, troot) or real == troot:
        return False
    rel = real[len(troot.rstrip(os.sep)) + 1:].split(os.sep)
    return len(rel) >= 2 and rel[1] == TASK_SUBDIR


def _sweep_dir(folder: str, cutoff: float) -> int:
    removed = 0
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    for name in names:
        if not name.endswith(".txt"):
            continue
        p = os.path.join(folder, name)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.unlink(p)
                removed += 1
        except OSError:
            continue
    try:
        if not os.listdir(folder):
            os.rmdir(folder)
    except OSError:
        pass
    return removed


def sweep_expired(*, now: float | None = None) -> int:
    """Delete offload files older than TTL_DAYS under the chat root and under
    every task workspace's .tool-outputs. Returns the number removed."""
    cutoff = (now if now is not None else time.time()) - TTL_DAYS * 86400
    removed = 0
    try:
        for sid in os.listdir(ROOT):
            removed += _sweep_dir(os.path.join(ROOT, sid), cutoff)
    except OSError:
        pass
    try:
        from tasks import workspace  # noqa: PLC0415
        for tid in os.listdir(workspace.ROOT):
            removed += _sweep_dir(os.path.join(workspace.ROOT, tid, TASK_SUBDIR), cutoff)
    except Exception:  # noqa: BLE001
        pass
    return removed


# ---------------------------------------------------------------------------
# FunctionTool wrapper: the one place every NATIVE tool result passes through.
# (MCP tools call postprocess() directly from mcp_client.client._wrap_tool.)
# ---------------------------------------------------------------------------

def is_wrapped(tool) -> bool:
    return bool(getattr(getattr(tool, "on_invoke_tool", None), "_nimoos_offload", False))


def wrap_tool_output(tool):
    """Return `tool` with its on_invoke_tool wrapped so oversized results are
    offloaded. Idempotent; non-FunctionTool objects are returned as-is."""
    inner = getattr(tool, "on_invoke_tool", None)
    if inner is None or not dataclasses.is_dataclass(tool):
        return tool
    if getattr(inner, "_nimoos_offload", False):
        return tool
    tool_name = str(getattr(tool, "name", "") or "")

    async def _wrapped(ctx, input_json):
        call_id = str(getattr(ctx, "tool_call_id", "") or "")
        token = CALL_ID_VAR.set(call_id)
        try:
            out = await inner(ctx, input_json)
        finally:
            CALL_ID_VAR.reset(token)
        try:
            return postprocess(out, tool_name=tool_name, call_id=call_id)
        except Exception:  # noqa: BLE001 — treatment must never eat a result
            _LOG.warning("tool_output: postprocess failed for %s", tool_name,
                         exc_info=True)
            return out

    _wrapped._nimoos_offload = True  # type: ignore[attr-defined]
    return dataclasses.replace(tool, on_invoke_tool=_wrapped)
