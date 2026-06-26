import asyncio
import atexit
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from typing import AsyncGenerator

_LOG = logging.getLogger("nimoos-agent")

_SKILL_ID_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")
_MAX_SKILL_MD_BYTES = 50 * 1024

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

import db as db_module
import memory_store
from agent import AgentRunner
from confirm import ConfirmManager
from openai import AsyncOpenAI
from run_sink import RunSink, load_events_from_db
import title_gen
from fs import paths as _fs_paths
from fs import ignore as _fs_ignore
from fs import staging as _fs_staging
from fs.snapshots import SnapshotStore
from attachments import upload as att_upload
from attachments.paths import build_storage_path
from profiles import PROFILES

# Ensure MCP cache dirs exist on the persistent volume at startup.
# The `if _d:` guard makes this a no-op when the env vars are unset (e.g. in tests).
for _d in (
    os.environ.get("NIMOOS_MCP_HOME"),
    os.environ.get("npm_config_cache"),
    os.environ.get("UV_CACHE_DIR"),
):
    if _d:
        os.makedirs(_d, exist_ok=True)

_DB_PATH = os.environ.get("AGENT_DB_PATH", str(db_module._DB_PATH))
_conn = db_module.init_db(_DB_PATH)
_confirm_mgr = ConfirmManager(_conn)
# Pass the same _confirm_mgr through so /confirm POSTs and skill register/wait
# operate on a single in-memory _pending dict.
_runner = AgentRunner(_conn, confirm_mgr=_confirm_mgr)

_SNAPSHOTS_ROOT = os.environ.get(
    "AGENT_SNAPSHOTS_ROOT", "/var/lib/nimoos/ai/agent/snapshots")
_snapshots_root = _SNAPSHOTS_ROOT  # exposed for tests to monkeypatch
_snapshot_store = SnapshotStore(root=_SNAPSHOTS_ROOT)


def _data_root() -> str:
    """Root directory for per-session attachment storage."""
    return os.environ.get(
        "NIMOOS_AGENT_DATA_ROOT",
        str(db_module._DB_PATH.parent),
    )


def _user_patterns_from_header(request: Request) -> list[str]:
    raw = request.headers.get("X-Agent-User-Blacklist", "")
    if not raw:
        return []
    try:
        decoded = base64.b64decode(raw).decode("utf-8")
        data = json.loads(decoded)
        if isinstance(data, list):
            return [str(s) for s in data][:1024]
    except Exception:
        return []
    return []


def _assert_owns_session(session_id: str, user_id: str) -> None:
    row = _conn.execute(
        "SELECT id FROM sessions WHERE id=? AND user_id=?",
        (session_id, user_id),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="session not found")

# session_id -> live RunSink. Stays populated after a run finishes so that a
# client reconnecting moments later can still replay the run; replaced when a
# new run starts on the same session.
_active_runs: dict[str, RunSink] = {}

# Heartbeat cadence. SSE clients (and intermediate proxies) idle-disconnect
# after ~30s without traffic; the comment-style heartbeat keeps the channel
# warm during long confirmation waits without polluting the event log.
_KEEPALIVE_SECONDS = 15


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean env var. Truthy values: '1', 'true', 'yes' (case-insensitive)."""
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes")


def _env_str(name: str, default: str) -> str:
    """Read a string env var with a default."""
    val = os.environ.get(name)
    if val is None:
        return default
    return val


MAX_ATTACHMENT_SIZE         = _env_int("NIMOOS_MAX_ATTACHMENT_SIZE",         524_288_000)
MAX_IMAGE_ATTACHMENT_SIZE   = _env_int("NIMOOS_MAX_IMAGE_ATTACHMENT_SIZE",   20_971_520)
MAX_ATTACHMENTS_PER_SESSION = _env_int("NIMOOS_MAX_ATTACHMENTS_PER_SESSION", 50)
MAX_ATTACHMENT_TEXT_CHARS   = _env_int("NIMOOS_MAX_ATTACHMENT_TEXT_CHARS",   32_768)
FFPROBE_TIMEOUT             = _env_int("NIMOOS_FFPROBE_TIMEOUT",             5)
ATTACHMENT_GC_AGE           = _env_int("NIMOOS_ATTACHMENT_GC_AGE",           86_400)
MAX_DOC_CHARS               = _env_int("NIMOOS_MAX_DOC_CHARS",               262_144)
MAX_DOC_EXTRACT_SECONDS     = _env_int("NIMOOS_MAX_DOC_EXTRACT_SECONDS",     8)
MAX_DOC_UNCOMPRESSED_BYTES  = _env_int("NIMOOS_MAX_DOC_UNCOMPRESSED_BYTES",  209_715_200)

# Egress DLP / sandbox execution mode
# NIMOOS_AGENT_EXEC_MODE: "netns" (default) or "none" (disable sandbox)
EXEC_MODE           = _env_str("NIMOOS_AGENT_EXEC_MODE",    "netns")
# Path to the egress-proxy binary
EGRESS_PROXY_BIN    = _env_str("NIMOOS_EGRESS_PROXY_BIN",   "/usr/local/bin/egress-proxy")
# TOFU: trust-on-first-use — proxy allows first connection per host by default
EGRESS_TOFU         = _env_bool("NIMOOS_EGRESS_TOFU",       True)
# Upload byte threshold above which egress is flagged for confirmation
EGRESS_UPLOAD_BYTES = _env_int("NIMOOS_EGRESS_UPLOAD_BYTES", 65_536)

# Subprocess handles for orchestrated children (executor + proxy).
# Populated by the startup handler; used by the shutdown handler.
_executor_proc: "subprocess.Popen | None" = None
_proxy_proc: "subprocess.Popen | None" = None

app = FastAPI(title="nimoos-agent")


# container-local liveness probe — intentionally minimal (no auth/DB)
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.on_event("startup")
async def _attachments_startup():
    """Run attachment GC at agent startup.

    Cleans up:
    - Draft attachments (message_id IS NULL) older than ATTACHMENT_GC_AGE seconds
    - Orphan session directories not matching any sessions.id row

    Errors during GC are logged and swallowed — they must never block startup.
    """
    try:
        from attachments.gc import run_startup_gc
        run_startup_gc(_db(), _data_root(),
                       age_seconds=ATTACHMENT_GC_AGE)
    except Exception as e:
        _LOG.warning("attachment GC failed: %s", e)


@app.on_event("startup")
async def _memory_worker_startup():
    import memory_extract
    memory_extract.start_worker(_db())


# ---------------------------------------------------------------------------
# Startup orchestration helpers (netns + egress-proxy + executor)
# ---------------------------------------------------------------------------

def _build_proxy_argv(
    proxy_bin: str,
    listen: str = "169.254.7.1:8888",
    dns: str = "169.254.7.1:53",
    confirm_url: str = "http://127.0.0.1:8282/internal/egress-confirm",
    grant_listen: str = "127.0.0.1:8889",
) -> list[str]:
    """Return the argv list for starting the egress-proxy.

    Extracted as a pure function so tests can verify argv construction without
    actually spawning a process.

    NOTE (P0 design): The grant_listen address exists for A-path (content-
    inspection allow-listing) but main.py does not call it in this phase — there
    is no content judge, so the grant channel has no caller.  Proxy grant support
    is wired at the process level; P1 will add the judge that calls it.
    """
    return [
        proxy_bin,
        "-listen", listen,
        "-dns", dns,
        "-confirm-url", confirm_url,
        "-grant-listen", grant_listen,
    ]


def _wait_for_pid_file(pid_file: str, timeout: float = 5.0, interval: float = 0.1) -> int:
    """Poll *pid_file* until it appears and contains a valid PID, or timeout.

    Returns the PID (int) on success; raises TimeoutError on timeout.
    Extracted as a pure function for unit-testing without subprocess.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with open(pid_file) as fh:
                content = fh.read().strip()
            if content:
                return int(content)
        except (FileNotFoundError, ValueError):
            pass
        time.sleep(interval)
    raise TimeoutError(f"executor pid file {pid_file!r} not ready after {timeout}s")


def _clean_stale_runtime(pid_file: str, sock_path: str, mcp_sock_dir: str) -> None:
    """Remove stale runtime artifacts left by a previous executor instance.

    The executor's pid/socket live on a *persistent* writable mount
    (/var/lib/nimoos/ai/agent), so they survive `docker restart`.  A stale pid
    file is dangerous: _wait_for_pid_file would read the PREVIOUS run's (dead,
    possibly recycled) PID and create_netns() would then move VETH_E into the
    wrong network namespace.  Deleting these before spawning the new executor
    guarantees _wait_for_pid_file can only return the fresh PID.

    Best-effort: every error is swallowed — cleanup must never block startup.
    """
    import glob

    for path in (pid_file, sock_path):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except Exception as exc:
            _LOG.warning("clean stale runtime: unlink %s: %s", path, exc)

    # Orphaned per-MCP-server sockets (agent-mcp-*.sock) accumulate across restarts.
    try:
        for p in glob.glob(os.path.join(mcp_sock_dir, "agent-mcp-*.sock")):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
            except Exception as exc:
                _LOG.warning("clean stale runtime: unlink %s: %s", p, exc)
    except Exception as exc:
        _LOG.warning("clean stale runtime: glob %s: %s", mcp_sock_dir, exc)


def _teardown_children() -> None:
    """Terminate executor and proxy subprocesses. Safe to call multiple times."""
    global _executor_proc, _proxy_proc
    for name, proc in (("executor", _executor_proc), ("proxy", _proxy_proc)):
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception as exc:
                _LOG.warning("teardown %s: %s", name, exc)
                try:
                    proc.kill()
                except Exception:
                    pass
    # Attempt netns teardown (requires CAP_NET_ADMIN; silently ignored otherwise)
    try:
        from netns import bootstrap
        bootstrap.teardown()
    except Exception as exc:
        _LOG.debug("netns teardown: %s", exc)
    _executor_proc = None
    _proxy_proc = None


@app.on_event("startup")
async def _egress_startup():
    """Fork executor, wire network namespace, and start egress-proxy.

    Only runs when EXEC_MODE == "netns".  All errors are caught and logged;
    the agent continues to start even if orchestration fails — this prevents a
    missing `ip` binary or non-root environment (e.g. CI) from hard-crashing
    the service.  In that case the executor sandbox is simply unavailable and
    shell/MCP tools will fail gracefully at runtime.
    """
    global _executor_proc, _proxy_proc

    if EXEC_MODE != "netns":
        _LOG.info("egress startup: EXEC_MODE=%r — skipping netns orchestration", EXEC_MODE)
        return

    pid_file = _env_str("NIMOOS_EXEC_PID_FILE", "/var/run/nimoos/agent-exec.pid")
    sock_path = _env_str("NIMOOS_EXEC_SOCK", "/var/run/nimoos/agent-exec.sock")
    mcp_sock_dir = _env_str("NIMOOS_MCP_SOCK_DIR", "/var/run/nimoos")

    try:
        # 0. Clear stale runtime artifacts from a previous (possibly SIGKILLed)
        #    executor.  These live on a persistent mount and survive restarts;
        #    a stale pid file would otherwise make _wait_for_pid_file return a
        #    dead/recycled PID and wire VETH_E into a foreign netns.
        _clean_stale_runtime(pid_file, sock_path, mcp_sock_dir)

        # 1. Fork executor (python -m netns.executor) from the agent root dir
        #    so that `import netns` resolves correctly.
        agent_root = os.path.dirname(os.path.abspath(__file__))
        _executor_proc = subprocess.Popen(
            [sys.executable, "-m", "netns.executor"],
            cwd=agent_root,
        )
        _LOG.info("egress startup: executor spawned pid=%d", _executor_proc.pid)

        # 2. Wait for executor to write its PID file (it runs unshare then writes)
        exec_pid = _wait_for_pid_file(pid_file, timeout=5.0)
        # Defense-in-depth: the executor shares our PID namespace (no pidns), so
        # the PID it writes MUST equal the child handle's pid.  A mismatch means
        # cleanup missed a stale file — refuse rather than wire veth into a
        # foreign/dead netns.
        if _executor_proc is None or exec_pid != _executor_proc.pid:
            raise RuntimeError(
                "stale pid file: read %s, expected %s"
                % (exec_pid, getattr(_executor_proc, "pid", None))
            )
        _LOG.info("egress startup: executor netns pid=%d", exec_pid)

        # 3. Create veth pair and configure the host side (requires CAP_NET_ADMIN)
        from netns import bootstrap
        bootstrap.create_netns(exec_pid)
        _LOG.info("egress startup: netns veth configured")

        # 4. Start egress-proxy
        proxy_argv = _build_proxy_argv(EGRESS_PROXY_BIN)
        _proxy_proc = subprocess.Popen(proxy_argv)
        _LOG.info("egress startup: proxy spawned pid=%d argv=%s",
                  _proxy_proc.pid, proxy_argv)

        # Register atexit teardown (also covered by the shutdown event below)
        atexit.register(_teardown_children)

    except Exception as exc:
        _LOG.error(
            "egress startup failed (%s) — sandbox unavailable, "
            "agent will still start without netns isolation",
            exc,
        )
        # Best-effort cleanup of any partially-started children
        try:
            _teardown_children()
        except Exception:
            pass


@app.on_event("shutdown")
async def _egress_shutdown():
    """Tear down executor and proxy on graceful shutdown."""
    try:
        _teardown_children()
    except Exception as exc:
        _LOG.warning("egress shutdown: %s", exc)


# ---------------------------------------------------------------------------
# Internal egress-confirm callback (called by egress-proxy, not by users)
# ---------------------------------------------------------------------------

class _EgressConfirmRequest(BaseModel):
    host: str
    bytes: int = 0
    reason: str = ""


@app.post("/internal/egress-confirm")
async def egress_confirm(req: _EgressConfirmRequest):
    """Receive an egress-confirmation request from the egress-proxy.

    The proxy calls this endpoint (bound to 127.0.0.1) when an outbound
    connection exceeds the upload-bytes threshold or matches a policy rule.
    We route the confirmation to the most-recently-active agent session.

    Routing policy (P0): take the last-active session sink. When multiple
    sessions are running concurrently, attribution is ambiguous; P1 will add
    per-connection session tagging in the proxy protocol.

    Fail-closed: any condition that prevents us from finding an active session
    or registering a confirmation returns {"allow": false}.
    """
    # Find an active session — P0 uses last-active heuristic
    session_id: str | None = _runner._last_active_session
    if session_id is not None and session_id not in _runner._active_sinks:
        # last-active session has since finished; check if any remain
        if _runner._active_sinks:
            session_id = next(iter(_runner._active_sinks))
        else:
            session_id = None

    if session_id is None:
        _LOG.debug("egress-confirm: no active session — fail-closed (host=%s)", req.host)
        return {"allow": False}

    sink = _runner._active_sinks.get(session_id)
    if sink is None:
        _LOG.debug("egress-confirm: sink gone for session %s — fail-closed", session_id)
        return {"allow": False}

    description = (
        f"Outbound connection to {req.host!r} — "
        f"bytes={req.bytes}, reason={req.reason or 'policy'}"
    )
    try:
        cid = _confirm_mgr.register(session_id, "egress", description, req.host)
        await sink.put({
            "type": "confirmation_required",
            "confirm_id": cid,
            "action": "egress_confirm",
            "host": req.host,
            "bytes": req.bytes,
            "reason": req.reason,
            "description": description,
        })
        granted = await _confirm_mgr.wait(cid)
        return {"allow": bool(granted)}
    except Exception as exc:
        _LOG.error("egress-confirm error for session %s: %s — fail-closed", session_id, exc)
        return {"allow": False}


from provider_adapters import ThinkingLevel as _ThinkingLevel


class ThinkingConfigPayload(BaseModel):
    enabled: bool
    level: _ThinkingLevel


class MaxTurnsPayload(BaseModel):
    max_turns: int = Field(ge=0)


class MemorySettingsPayload(BaseModel):
    enabled: bool


class ContextPhoto(BaseModel):
    id: str
    name: str = ""
    takenAt: str = ""
    place: str = ""


class ContextAlbum(BaseModel):
    id: str
    name: str = ""


class CreateSessionRequest(BaseModel):
    agent_type: str = "general"


class RunRequest(BaseModel):
    message: str = ""
    model: str = ""
    kind: str = "chat"          # 'chat' | 'init'
    init_target: str | None = None
    thinking: ThinkingConfigPayload | None = None
    attachment_ids: list[str] = []
    context_photo: ContextPhoto | None = None
    continue_run: bool = False
    context_album: ContextAlbum | None = None


class SandboxRunRequest(BaseModel):
    skill_id: str
    prompt: str
    network: bool = False  # informational; --unshare-net is forced for sandbox


class ConfirmRequest(BaseModel):
    confirmed: bool = True
    remember: bool = False

    model_config = {"extra": "ignore"}


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Return 401 when X-User-Id header is missing
    for error in exc.errors():
        if error.get("loc") and "x-user-id" in str(error["loc"]).lower():
            return JSONResponse(status_code=401, content={"detail": "X-User-Id header required"})
    return await request_validation_exception_handler(request, exc)


@app.get("/agent/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# /agent/fs/list
# ---------------------------------------------------------------------------

@app.get("/agent/fs/list")
async def fs_list(
    path: str,
    request: Request,
    show_ignored: int = 0,
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    abs_ = os.path.abspath(path)
    if not os.path.isdir(abs_):
        raise HTTPException(status_code=404, detail="not a directory")
    user_patterns = _user_patterns_from_header(request)

    out = []
    try:
        entries = list(os.scandir(abs_))
    except PermissionError:
        raise HTTPException(status_code=403, detail="permission denied")
    for entry in sorted(entries, key=lambda e: e.name):
        item = {
            "name": entry.name,
            "path": entry.path,
            "kind": "dir" if entry.is_dir(follow_symlinks=False) else "file",
            "ignored": False,
            "ignored_reason": None,
        }
        try:
            st = entry.stat(follow_symlinks=False)
            item["size"] = st.st_size
            item["modified"] = int(st.st_mtime)
        except OSError:
            item["size"] = None
            item["modified"] = None
        try:
            _fs_ignore.gate(entry.path, [], user_patterns,
                            allow_gitignore_override=False)
        except _fs_ignore.BlockedImplicit:
            continue
        except _fs_ignore.BlockedHardBlacklist:
            continue
        except _fs_ignore.BlockedGitignore:
            if not show_ignored:
                continue
            item["ignored"] = True
            item["ignored_reason"] = "gitignore"
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Visible-resources endpoints
# ---------------------------------------------------------------------------

class VisibleResourceCreate(BaseModel):
    path: str
    kind: str = "folder"  # 'folder' | 'file'
    force: bool = False


@app.get("/agent/sessions/{session_id}/visible-resources")
async def list_visible_resources(
    session_id: str,
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    _assert_owns_session(session_id, x_user_id)
    rows = _conn.execute(
        "SELECT id, path, kind, added_at FROM visible_resources "
        "WHERE session_id=? ORDER BY added_at",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


@app.post("/agent/sessions/{session_id}/visible-resources")
async def add_visible_resource(
    session_id: str,
    body: VisibleResourceCreate,
    request: Request,
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    _assert_owns_session(session_id, x_user_id)
    if _session_agent_type(session_id) != "general":
        # Restricted profiles have no filesystem layer; this endpoint is
        # meaningless for them and stays closed as an attack surface.
        raise HTTPException(status_code=400,
                            detail="visible-resources not supported for this agent profile")
    if body.kind not in ("folder", "file"):
        raise HTTPException(status_code=400, detail="kind must be folder|file")
    abs_ = os.path.abspath(body.path)
    if not os.path.exists(abs_):
        raise HTTPException(status_code=404, detail="path does not exist")
    if (body.kind == "folder") != os.path.isdir(abs_):
        raise HTTPException(status_code=400, detail="kind/type mismatch")

    user_patterns = _user_patterns_from_header(request)
    # For directories we probe with a trailing slash so that directory-anchored
    # blacklist patterns (e.g. "/etc/") are correctly matched by pathspec.
    gate_path = (abs_ + "/") if body.kind == "folder" else abs_
    try:
        _fs_ignore.gate(gate_path, [], user_patterns,
                        allow_gitignore_override=body.force)
    except _fs_ignore.BlockedImplicit:
        raise HTTPException(status_code=403, detail="implicit ignore")
    except _fs_ignore.BlockedHardBlacklist:
        raise HTTPException(status_code=403, detail="hard blacklist")
    except _fs_ignore.BlockedGitignore:
        raise HTTPException(status_code=409,
                            detail="gitignore: pass force=true to override")

    # Reject if agent currently busy on this session
    from agent import _get_lock
    if _get_lock(session_id).locked():
        raise HTTPException(status_code=409, detail="agent_busy")

    cur = _conn.execute(
        "INSERT INTO visible_resources (session_id, path, kind, added_at) "
        "VALUES (?,?,?,?) ON CONFLICT(session_id, path) DO NOTHING",
        (session_id, abs_, body.kind, int(time.time())),
    )
    _conn.commit()
    rid = cur.lastrowid
    return {"id": rid, "path": abs_, "kind": body.kind}


@app.delete("/agent/sessions/{session_id}/visible-resources/{res_id}")
async def remove_visible_resource(
    session_id: str,
    res_id: int,
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    _assert_owns_session(session_id, x_user_id)
    from agent import _get_lock
    if _get_lock(session_id).locked():
        raise HTTPException(status_code=409, detail="agent_busy")
    _conn.execute(
        "DELETE FROM visible_resources WHERE id=? AND session_id=?",
        (res_id, session_id),
    )
    _conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Attachments endpoints
# ---------------------------------------------------------------------------

@app.post("/agent/sessions/{session_id}/attachments", status_code=201)
async def upload_attachment(
    session_id: str,
    file: UploadFile = File(...),
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    _assert_owns_session(session_id, x_user_id)
    conn = _db()

    cnt = conn.execute(
        "SELECT COUNT(*) FROM attachments WHERE session_id=?", (session_id,)
    ).fetchone()[0]
    if cnt >= MAX_ATTACHMENTS_PER_SESSION:
        raise HTTPException(status_code=409,
                            detail="MaxAttachmentsPerSession reached")

    # Stream to a temp .part path; handle_upload then renames to att_xxx__name
    tmp_id = uuid.uuid4().hex
    part_path = build_storage_path(_data_root(), session_id, tmp_id, "upload.part")
    os.makedirs(os.path.dirname(part_path), exist_ok=True)

    size = await att_upload.stream_to_disk(file, part_path, MAX_ATTACHMENT_SIZE)

    result = await att_upload.handle_upload(
        conn=conn,
        data_root=_data_root(),
        session_id=session_id,
        original_name=file.filename or "untitled",
        part_path=part_path,
        size=size,
        max_image_size=MAX_IMAGE_ATTACHMENT_SIZE,
        ffprobe_timeout=FFPROBE_TIMEOUT,
        max_doc_chars=MAX_DOC_CHARS,
        max_doc_uncompressed_bytes=MAX_DOC_UNCOMPRESSED_BYTES,
        max_extract_seconds=MAX_DOC_EXTRACT_SECONDS,
    )
    return result


@app.get("/agent/sessions/{session_id}/attachments")
async def list_attachments(
    session_id: str,
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    _assert_owns_session(session_id, x_user_id)
    rows = _db().execute(
        "SELECT id, message_id, filename, mime, kind, size_bytes, "
        "       meta_json, created_at "
        "FROM attachments WHERE session_id = ? ORDER BY created_at",
        (session_id,)
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "message_id": r["message_id"],
            "filename": r["filename"],
            "mime": r["mime"],
            "kind": r["kind"],
            "size_bytes": r["size_bytes"],
            "meta": json.loads(r["meta_json"]) if r["meta_json"] else None,
            "created_at": r["created_at"],
        })
    return out


@app.delete("/agent/sessions/{session_id}/attachments/{attachment_id}")
async def delete_attachment(
    session_id: str,
    attachment_id: str,
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    _assert_owns_session(session_id, x_user_id)
    row = _db().execute(
        "SELECT message_id, rel_path FROM attachments "
        "WHERE id = ? AND session_id = ?", (attachment_id, session_id)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if row["message_id"] is not None:
        raise HTTPException(status_code=409, detail="already bound to message")
    full = os.path.join(_data_root(), "sessions", session_id,
                        "attachments", row["rel_path"])
    try:
        os.remove(full)
    except FileNotFoundError:
        pass
    _db().execute(
        "DELETE FROM attachments WHERE id = ? AND session_id = ?",
        (attachment_id, session_id))
    _db().commit()
    return {"ok": True}


@app.get("/agent/sessions/{session_id}/attachments/{attachment_id}/raw")
async def download_attachment(
    session_id: str,
    attachment_id: str,
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    _assert_owns_session(session_id, x_user_id)
    row = _db().execute(
        "SELECT filename, mime, rel_path FROM attachments "
        "WHERE id = ? AND session_id = ?", (attachment_id, session_id)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    full = os.path.join(_data_root(), "sessions", session_id,
                        "attachments", row["rel_path"])
    return FileResponse(
        full, media_type=row["mime"],
        headers={"Content-Disposition":
                 f'inline; filename="{row["filename"]}"'},
    )


# ---------------------------------------------------------------------------
# Staged-changes endpoints
# ---------------------------------------------------------------------------

@app.get("/agent/sessions/{session_id}/staged-changes")
async def list_staged_changes(
    session_id: str,
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    _assert_owns_session(session_id, x_user_id)
    rows = _conn.execute(
        "SELECT id, run_id, seq, op, path, dst_path, snapshot_path, "
        "       size_bytes, status, created_at "
        "FROM staged_changes "
        "WHERE session_id=? AND status IN ('pending','orphan') "
        "ORDER BY run_id, seq",
        (session_id,),
    ).fetchall()
    grouped: dict[str, dict] = {}
    for r in rows:
        sm = (bool(r["snapshot_path"]) and not os.path.exists(r["snapshot_path"])) \
             or r["status"] == "orphan"
        run = grouped.setdefault(r["run_id"], {
            "run_id": r["run_id"], "created_at": r["created_at"], "items": []
        })
        run["items"].append({
            "seq": r["seq"], "op": r["op"], "path": r["path"],
            "dst_path": r["dst_path"], "size_bytes": r["size_bytes"],
            "snapshot_missing": sm,
        })
    return list(grouped.values())


@app.post("/agent/sessions/{session_id}/staged-changes/commit")
async def commit_staged(
    session_id: str,
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    _assert_owns_session(session_id, x_user_id)
    from agent import _get_lock
    if _get_lock(session_id).locked():
        raise HTTPException(status_code=409, detail="agent_busy")
    _fs_staging.commit_session(_conn, _snapshot_store, session_id)
    return {"ok": True}


@app.post("/agent/sessions/{session_id}/staged-changes/runs/{run_id}/revert")
async def revert_run(
    session_id: str,
    run_id: str,
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    _assert_owns_session(session_id, x_user_id)
    from agent import _get_lock
    if _get_lock(session_id).locked():
        raise HTTPException(status_code=409, detail="agent_busy")
    res = _fs_staging.revert_run(_conn, _snapshot_store, session_id, run_id)
    if res["status"] == "snapshot_missing":
        raise HTTPException(status_code=409, detail="snapshot_missing")
    if res["status"] == "partial":
        return JSONResponse(status_code=207, content=res)
    return res


class RevertRequest(BaseModel):
    batch_id: str | None = None
    staged_ids: list[int] | None = None


@app.post("/agent/sessions/{session_id}/revert")
async def revert_changes(
    session_id: str,
    req: RevertRequest,
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    _assert_owns_session(session_id, x_user_id)
    if req.batch_id:
        return _fs_staging.revert_batch(_conn, _snapshot_store, session_id, req.batch_id)
    if req.staged_ids:
        return _fs_staging.revert_items(_conn, _snapshot_store, session_id, req.staged_ids)
    return {"status": "nothing_to_revert"}


@app.post("/agent/sessions")
async def create_session(body: CreateSessionRequest | None = None,
                         x_user_id: str = Header(..., alias="X-User-Id")):
    agent_type = body.agent_type if body is not None else "general"
    if agent_type not in PROFILES:
        # Fixed message on purpose: echoing the input would let callers
        # probe which agent_type values exist.
        raise HTTPException(status_code=422, detail="invalid agent_type")
    session_id = str(uuid.uuid4())
    now = int(time.time())
    _db().execute(
        "INSERT INTO sessions (id, user_id, title, created_at, updated_at, agent_type) "
        "VALUES (?,?,?,?,?,?)",
        (session_id, x_user_id, None, now, now, agent_type)
    )
    _db().commit()
    return {"session_id": session_id, "agent_type": agent_type}


def _session_agent_type(session_id: str) -> str:
    """Resolve a session's agent_type; missing row/value = 'general'."""
    row = _db().execute(
        "SELECT agent_type FROM sessions WHERE id=?", (session_id,)
    ).fetchone()
    if not row or not row["agent_type"]:
        return "general"
    return row["agent_type"]


@app.get("/agent/sessions")
async def list_sessions(x_user_id: str = Header(..., alias="X-User-Id")):
    rows = _db().execute(
        "SELECT id, title, created_at, updated_at, agent_type "
        "FROM sessions WHERE user_id=? ORDER BY updated_at DESC",
        (x_user_id,)
    ).fetchall()
    return [dict(row) for row in rows]


@app.delete("/agent/sessions/{session_id}")
async def delete_session(session_id: str, x_user_id: str = Header(..., alias="X-User-Id")):
    _conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    _conn.execute("DELETE FROM sessions WHERE id=? AND user_id=?", (session_id, x_user_id))
    _conn.commit()
    shutil.rmtree(os.path.join(_snapshots_root, session_id), ignore_errors=True)
    return {"ok": True}


@app.get("/agent/sessions/{session_id}/messages")
async def list_messages(session_id: str, x_user_id: str = Header(..., alias="X-User-Id")):
    row = _conn.execute("SELECT id FROM sessions WHERE id=? AND user_id=?",
                        (session_id, x_user_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="session not found")

    last_row = _conn.execute(
        "SELECT content FROM messages WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
        (session_id,)
    ).fetchone()
    if not last_row:
        return []

    try:
        history = json.loads(last_row["content"])
    except (json.JSONDecodeError, KeyError):
        return []

    messages = _hydrate_messages(history, session_id_for_urls=session_id)
    messages = _inject_access_request_cards(messages, session_id, _conn)
    return _enrich_with_attachments(messages, session_id=session_id, conn=_conn)


def _enrich_with_attachments(messages: list, *, session_id: str, conn) -> list:
    """Backfill user messages with their attachments.

    The SDK input list only includes image attachments as input_image blocks;
    documents and other non-image attachments are passed to the model via the
    system prompt and read_attachment tool, so they don't appear in the
    history JSON. To make historical sessions render correctly, walk the
    attachments table for this session, group by message_id in chronological
    order, and assign each group to the N-th user turn that originally had
    attachments (detected as a hydrated message with a `blocks` field rather
    than a `content` string — `build_user_content` returns a list iff
    attachment_ids is non-empty, which becomes a blocks-shaped hydrated
    message).

    For images already represented as input_image blocks, just backfill the
    filename + mime. For non-image attachments, append a new
    `{type:"attachment", attachment_id, kind, filename, mime, url}` block.
    """
    if not messages:
        return messages
    rows = conn.execute(
        "SELECT message_id FROM attachments "
        "WHERE session_id=? AND message_id IS NOT NULL "
        "GROUP BY message_id ORDER BY MIN(created_at)",
        (session_id,),
    ).fetchall()
    ordered_msg_ids = [r["message_id"] for r in rows]
    if not ordered_msg_ids:
        return messages

    idx = 0
    for msg in messages:
        if msg.get("role") != "user":
            continue
        # blocks-shaped (vs content-shaped) ⇔ original turn had attachments
        if not isinstance(msg.get("blocks"), list):
            continue
        if idx >= len(ordered_msg_ids):
            break
        msg_id = ordered_msg_ids[idx]
        idx += 1
        atts = conn.execute(
            "SELECT id, filename, mime, kind FROM attachments "
            "WHERE message_id=? AND session_id=? ORDER BY created_at",
            (msg_id, session_id),
        ).fetchall()
        existing_aids = {b.get("attachment_id") for b in msg["blocks"]
                         if b.get("attachment_id")}
        for r in atts:
            aid = r["id"]
            if aid in existing_aids:
                # Image already represented as input_image; backfill the
                # filename + mime so the UI can show a meaningful chip.
                for b in msg["blocks"]:
                    if b.get("attachment_id") == aid:
                        b.setdefault("filename", r["filename"])
                        b.setdefault("mime", r["mime"])
                continue
            url = (f"/v1/ai/agent/sessions/{session_id}/attachments/{aid}/raw")
            if r["kind"] == "image":
                msg["blocks"].append({
                    "type": "image",
                    "attachment_id": aid,
                    "url": url,
                    "filename": r["filename"],
                    "mime": r["mime"],
                })
            else:
                msg["blocks"].append({
                    "type": "attachment",
                    "attachment_id": aid,
                    "kind": r["kind"],
                    "url": url,
                    "filename": r["filename"],
                    "mime": r["mime"],
                })
    return messages


def _flatten_content(content) -> str:
    # User input: plain string. Assistant output (Agents SDK to_input_list):
    # list of blocks like [{"type": "output_text", "text": "..."}].
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in (
                    "output_text", "text", "input_text", "reasoning_text"):
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


def _hydrate_messages(history: list, session_id_for_urls: str | None = None) -> list:
    # Translate Agents SDK to_input_list() into the UI's per-message shape:
    #   user      → {role:'user', content:str}
    #   assistant → {role:'assistant', blocks:[thinking|tool|md]} grouped per turn,
    # so AssistantMessage.vue's BlockRenderer can replay the run as it happened.
    result = []
    state = {"counter": 0, "current": None, "pending": {}}

    def new_id(prefix: str) -> str:
        state["counter"] += 1
        return f"h-{prefix}-{state['counter']}"

    def flush():
        if state["current"] and state["current"]["blocks"]:
            result.append(state["current"])
        state["current"] = None
        state["pending"] = {}

    def blocks():
        if state["current"] is None:
            state["current"] = {
                "id": new_id("a"),
                "role": "assistant",
                "blocks": [],
                "streaming": False,
            }
        return state["current"]["blocks"]

    for item in history:
        item_type = item.get("type")
        role = item.get("role")

        if role == "user":
            flush()
            content = item.get("content")
            if isinstance(content, list):
                # User turn with attachments — emit a structured message that
                # carries both text and image refs for UI rendering.
                ui_blocks = []
                for blk in content:
                    if (isinstance(blk, dict)
                            and blk.get("type") == "input_image"
                            and "attachment_id" in blk):
                        aid = blk["attachment_id"]
                        url = (
                            f"/v1/ai/agent/sessions/{session_id_for_urls}"
                            f"/attachments/{aid}/raw"
                            if session_id_for_urls else None
                        )
                        ui_blocks.append({
                            "type": "image",
                            "attachment_id": aid,
                            "url": url,
                        })
                    elif (isinstance(blk, dict)
                            and blk.get("type") in ("input_text", "text")):
                        text = blk.get("text", "")
                        if text:
                            ui_blocks.append({"type": "text", "text": text})
                if ui_blocks:
                    result.append({
                        "id": new_id("u"),
                        "role": "user",
                        "blocks": ui_blocks,
                    })
                continue
            # Backward compat: string content
            text = _flatten_content(content)
            if text:
                result.append({"id": new_id("u"), "role": "user", "content": text})
            continue

        if item_type == "reasoning":
            text = _flatten_content(item.get("content"))
            if text:
                blocks().append({
                    "type": "thinking",
                    "text": text,
                    "streaming": False,
                    "defaultOpen": False,
                })

        elif item_type == "function_call":
            name = item.get("name", "tool")
            args_raw = item.get("arguments") or ""
            try:
                parsed = json.loads(args_raw) if args_raw else {}
                args_pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
                preview = json.dumps(parsed, ensure_ascii=False)
            except (ValueError, TypeError):
                args_pretty = str(args_raw)
                preview = args_pretty
            bs = blocks()
            bs.append({
                "type": "tool",
                "state": "success",
                "name": name,
                "argsPreview": preview[:80],
                "sections": [{"label": "ARGUMENTS", "code": args_pretty}],
            })
            call_id = item.get("call_id") or item.get("id")
            if call_id and call_id != "__fake_id__":
                state["pending"][call_id] = len(bs) - 1

        elif item_type == "function_call_output":
            output = str(item.get("output", ""))
            call_id = item.get("call_id") or item.get("id")
            bs = blocks()
            target = state["pending"].pop(call_id, None) if call_id and call_id != "__fake_id__" else None
            if target is None:
                # Fallback: most recent tool block without a RESULT section yet
                for i in range(len(bs) - 1, -1, -1):
                    blk = bs[i]
                    if blk.get("type") == "tool" and not any(
                            s.get("label") == "RESULT" for s in blk.get("sections", [])):
                        target = i
                        break
            if target is not None:
                bs[target].setdefault("sections", []).append({"label": "RESULT", "code": output})
            else:
                # Orphan output — still surface it so the user sees the result
                bs.append({
                    "type": "tool",
                    "state": "success",
                    "name": "result",
                    "sections": [{"label": "RESULT", "code": output}],
                })

        elif item_type == "message" and role == "assistant":
            text = _flatten_content(item.get("content"))
            if text:
                blocks().append({"type": "md", "text": text})

    flush()
    return result


def _inject_access_request_cards(messages: list, session_id: str, conn) -> list:
    """Rebuild resolved file-access permission cards into the loaded history.

    access_request events are UI-only (never part of the SDK history), so they'd
    vanish on refresh. We persist each request + decision in access_requests and
    re-attach the resolved cards here. Correlation: the k-th run that produced
    access requests maps to the k-th assistant turn (one run ≈ one assistant
    turn). Only granted/denied are shown; pending/cancelled are skipped.
    """
    rows = conn.execute(
        "SELECT confirm_id, run_id, path, kind, reason, decision "
        "FROM access_requests "
        "WHERE session_id=? AND decision IN ('granted','denied') "
        "ORDER BY created_at ASC",
        (session_id,),
    ).fetchall()
    if not rows:
        return messages

    # Group rows by run_id, preserving first-seen (created_at) order.
    groups: list[list] = []
    seen: dict[str, int] = {}
    for r in rows:
        rid = r["run_id"]
        if rid not in seen:
            seen[rid] = len(groups)
            groups.append([])
        groups[seen[rid]].append(r)

    assistant_turns = [m for m in messages if m.get("role") == "assistant"]
    if not assistant_turns:
        synthetic = {"id": "h-a-access", "role": "assistant",
                     "blocks": [], "streaming": False}
        messages.append(synthetic)
        assistant_turns = [synthetic]

    for gi, group in enumerate(groups):
        turn = assistant_turns[gi] if gi < len(assistant_turns) else assistant_turns[-1]
        bs = turn.setdefault("blocks", [])
        cards = [{
            "type": "access_request",
            "confirmId": r["confirm_id"],
            "path": r["path"],
            "kind": r["kind"],
            "reason": r["reason"],
            "decided": True,
            "granted": r["decision"] == "granted",
        } for r in group]
        # The access request happened right before the file operation it gated,
        # so place the card(s) before the first tool block of the turn (after any
        # leading text/thinking), not dangling at the very end of the turn.
        insert_at = next((i for i, b in enumerate(bs) if b.get("type") == "tool"), len(bs))
        bs[insert_at:insert_at] = cards
    return messages


class TitleUpdate(BaseModel):
    title: str


@app.patch("/agent/sessions/{session_id}/title")
async def update_title(
    session_id: str,
    body: TitleUpdate,
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="title must not be empty")
    title = title[:title_gen.TITLE_MAX_CHARS]
    now = int(time.time())
    cur = _conn.execute(
        "UPDATE sessions SET title=?, updated_at=? WHERE id=? AND user_id=?",
        (title, now, session_id, x_user_id),
    )
    _conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="session not found")
    return {"title": title, "updated_at": now}


class RegenerateTitleRequest(BaseModel):
    model: str = ""


@app.post("/agent/sessions/{session_id}/regenerate-title")
async def regenerate_title(
    session_id: str,
    body: RegenerateTitleRequest,
    x_user_id: str = Header(..., alias="X-User-Id"),
    x_agent_provider_key: str = Header("", alias="X-Agent-Provider-Key"),
    x_agent_provider_url: str = Header("", alias="X-Agent-Provider-Url"),
):
    row = _conn.execute(
        "SELECT id FROM sessions WHERE id=? AND user_id=?",
        (session_id, x_user_id),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="session not found")

    last_row = _conn.execute(
        "SELECT content FROM messages WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    history = []
    if last_row:
        try:
            history = json.loads(last_row["content"]) or []
        except (json.JSONDecodeError, TypeError):
            history = []

    fallback_title = title_gen.first_user_fallback(history)

    def _persist(title: str, fallback: bool):
        if not title:
            return {"title": "", "fallback": True}
        now = int(time.time())
        _conn.execute(
            "UPDATE sessions SET title=?, updated_at=? WHERE id=? AND user_id=?",
            (title[:title_gen.TITLE_MAX_CHARS], now, session_id, x_user_id),
        )
        _conn.commit()
        return {"title": title[:title_gen.TITLE_MAX_CHARS], "fallback": fallback}

    model = (body.model or "").strip()
    if not model or not history:
        return _persist(fallback_title, True)

    excerpt = title_gen.extract_history_excerpt(history)
    if not excerpt:
        return _persist(fallback_title, True)

    try:
        client = AsyncOpenAI(base_url=x_agent_provider_url, api_key=x_agent_provider_key)
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": title_gen.SYSTEM_PROMPT},
                    {"role": "user", "content": excerpt},
                ],
                temperature=0.3,
                max_tokens=256,
            ),
            timeout=15.0,
        )
        raw = ""
        if resp.choices:
            msg = resp.choices[0].message
            raw = (
                getattr(msg, "content", None)
                or getattr(msg, "reasoning_content", None)
                or ""
            )
        cleaned = title_gen.clean_llm_title(raw or "")
        if not cleaned:
            print(
                f"[regenerate_title] LLM returned no usable content "
                f"for session={session_id} model={model} raw_len={len(raw)}",
                flush=True,
            )
            return _persist(fallback_title, True)
        return _persist(cleaned, False)
    except (asyncio.TimeoutError, Exception):
        return _persist(fallback_title, True)


async def _stream_from_sink(sink: RunSink, prefix: list[dict] | None = None) -> AsyncGenerator[str, None]:
    """SSE generator that replays past events for `sink`, then live-tails.

    `prefix` is an optional list of synthetic events emitted before any real
    sink events. The /run-stream resume endpoint uses this to surface the
    user_message that triggered the run, since the frontend (which is just
    coming back to this session) doesn't have it locally.
    """
    if prefix:
        for ev in prefix:
            yield f"data: {json.dumps(ev)}\n\n"
    past, sub = sink.subscribe()
    try:
        for ev in past:
            yield f"data: {json.dumps(ev)}\n\n"
            if ev.get("type") == "done":
                # Past events already include a terminal 'done' (run finished
                # before this client connected). Stop after replay; no future
                # events will come.
                return
        while True:
            try:
                event = await asyncio.wait_for(sub.get(), timeout=_KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                # Comment line in SSE syntax — clients ignore it but the bytes
                # keep proxies and browsers from idle-closing the stream.
                yield ": ka\n\n"
                continue
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") == "done":
                return
    finally:
        sink.unsubscribe(sub)


def _stream_replay_only(events: list[dict], prefix: list[dict] | None = None) -> AsyncGenerator[str, None]:
    """SSE generator for a run whose in-memory sink is gone (process restart).
    Replays the persisted log; if no terminal event was ever logged, appends
    a synthetic one so the client unblocks instead of hanging."""
    async def gen():
        if prefix:
            for ev in prefix:
                yield f"data: {json.dumps(ev)}\n\n"
        saw_done = False
        for ev in events:
            yield f"data: {json.dumps(ev)}\n\n"
            if ev.get("type") == "done":
                saw_done = True
        if not saw_done:
            yield 'data: {"type": "error", "content": "agent process restarted; this run was interrupted"}\n\n'
            yield 'data: {"type": "done"}\n\n'
    return gen()


_db_cache: dict[str, object] = {}
_DB_PATH_INITIAL = _DB_PATH  # the path _conn was opened against at import time


def _db():
    """Return a SQLite connection for the current _DB_PATH.

    - When _DB_PATH hasn't been monkeypatched (equals the original import-time
      value), return the shared _conn so existing endpoints and tests that swap
      _conn directly keep working.
    - When _DB_PATH has been monkeypatched to a different path (as the new
      settings-endpoint tests do), open and cache a fresh connection against
      that path so each test gets an isolated DB.
    """
    path = _DB_PATH
    if path == _DB_PATH_INITIAL:
        return _conn
    if path not in _db_cache:
        _db_cache[path] = db_module.init_db(path)
    return _db_cache[path]


def _read_user_thinking_defaults(conn, user_id: str):
    """Return ThinkingConfig from user_settings, or hard-coded default."""
    from provider_adapters import ThinkingConfig
    row = conn.execute(
        "SELECT value FROM user_settings WHERE user_id=? AND key='thinking_default'",
        (user_id,),
    ).fetchone()
    if not row:
        return ThinkingConfig(enabled=True, level=_ThinkingLevel.MEDIUM)
    try:
        v = json.loads(row["value"])
        return ThinkingConfig(
            enabled=bool(v.get("enabled", True)),
            level=_ThinkingLevel(v.get("level", "medium")),
        )
    except (json.JSONDecodeError, ValueError):
        return ThinkingConfig(enabled=True, level=_ThinkingLevel.MEDIUM)


def _read_max_turns_setting(conn, user_id: str) -> int:
    """Raw user-level max_turns setting. 0 = unlimited; default 10.
    Negative / non-integer values fall back to 10."""
    row = conn.execute(
        "SELECT value FROM user_settings WHERE user_id=? AND key='max_turns_default'",
        (user_id,),
    ).fetchone()
    if not row:
        return 10
    try:
        v = int(row["value"])
    except (ValueError, TypeError):
        return 10
    return v if v >= 0 else 10


@app.get("/agent/user-settings/thinking")
async def get_thinking_defaults(request: Request):
    user_id = request.headers.get("X-User-Id", "")
    cfg = _read_user_thinking_defaults(_db(), user_id)
    return {"enabled": cfg.enabled, "level": cfg.level.value}


@app.put("/agent/user-settings/thinking")
async def put_thinking_defaults(request: Request, body: ThinkingConfigPayload):
    user_id = request.headers.get("X-User-Id", "")
    conn = _db()
    conn.execute(
        "INSERT INTO user_settings(user_id, key, value, updated_at) "
        "VALUES(?, 'thinking_default', ?, ?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, "
        "updated_at=excluded.updated_at",
        (user_id, json.dumps({"enabled": body.enabled,
                              "level": body.level.value}),
         int(time.time())),
    )
    conn.commit()
    return {"ok": True}


@app.get("/agent/user-settings/max-turns")
async def get_max_turns(request: Request):
    user_id = request.headers.get("X-User-Id", "")
    return {"max_turns": _read_max_turns_setting(_db(), user_id)}


@app.put("/agent/user-settings/max-turns")
async def put_max_turns(request: Request, body: MaxTurnsPayload):
    user_id = request.headers.get("X-User-Id", "")
    conn = _db()
    conn.execute(
        "INSERT INTO user_settings(user_id, key, value, updated_at) "
        "VALUES(?, 'max_turns_default', ?, ?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, "
        "updated_at=excluded.updated_at",
        (user_id, str(body.max_turns), int(time.time())),
    )
    conn.commit()
    return {"ok": True}


@app.get("/agent/user-memory")
async def list_user_memory(request: Request):
    user_id = request.headers.get("X-User-Id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="X-User-Id required")
    conn = _db()
    rows = memory_store.list_active(conn, user_id)
    ranked = memory_store.rank_for_injection(rows, int(time.time()))
    return [
        {"id": r["id"], "kind": r["kind"], "text": r["text"],
         "source": r["source"], "priority": r["priority"],
         "recall_count": r["recall_count"], "updated_at": r["updated_at"]}
        for r in ranked
    ]


@app.delete("/agent/user-memory/{mem_id}")
async def delete_user_memory(mem_id: str, request: Request):
    user_id = request.headers.get("X-User-Id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="X-User-Id required")
    conn = _db()
    row = conn.execute(
        "SELECT id FROM memory_entries "
        "WHERE id=? AND user_id=? AND status='active'",
        (mem_id, user_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    memory_store.disable_memory(conn, mem_id)
    return {"status": "deleted", "id": mem_id}


@app.get("/agent/user-memory/settings")
async def get_memory_settings(request: Request):
    user_id = request.headers.get("X-User-Id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="X-User-Id required")
    return {"enabled": memory_store.is_memory_enabled(_db(), user_id)}


@app.put("/agent/user-memory/settings")
async def put_memory_settings(request: Request, body: MemorySettingsPayload):
    user_id = request.headers.get("X-User-Id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="X-User-Id required")
    conn = _db()
    conn.execute(
        "INSERT INTO user_settings(user_id, key, value, updated_at) "
        "VALUES(?, 'memory_enabled', ?, ?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, "
        "updated_at=excluded.updated_at",
        (user_id, "1" if body.enabled else "0", int(time.time())),
    )
    conn.commit()
    return {"enabled": body.enabled}


@app.get("/agent/sessions/{session_id}/thinking")
async def get_session_thinking(session_id: str, request: Request):
    """Return the session's thinking override, or null fields if unset.

    The frontend calls this on session load so it can show the user the
    actual saved enabled/level for the session (falling back to user-level
    defaults if NULL). 404 only when the session doesn't exist for this user.
    """
    user_id = request.headers.get("X-User-Id", "")
    row = _db().execute(
        "SELECT thinking_enabled, thinking_level FROM sessions "
        "WHERE id=? AND user_id=?",
        (session_id, user_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "session not found")
    return {
        "thinking_enabled": (None if row["thinking_enabled"] is None
                             else bool(row["thinking_enabled"])),
        "thinking_level": row["thinking_level"],
    }


@app.patch("/agent/sessions/{session_id}/thinking")
async def patch_session_thinking(session_id: str, request: Request,
                                  body: ThinkingConfigPayload):
    user_id = request.headers.get("X-User-Id", "")
    conn = _db()
    cur = conn.execute(
        "UPDATE sessions SET thinking_enabled=?, thinking_level=?, updated_at=? "
        "WHERE id=? AND user_id=?",
        (1 if body.enabled else 0, body.level.value,
         int(time.time()), session_id, user_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "session not found")
    return {"ok": True}


def _start_run(session_id: str, user_id: str, message: str,
               provider_key: str, provider_url: str, model: str,
               *, kind: str = "chat", chat_username: str = "",
               user_patterns: list[str] | None = None,
               thinking=None, provider_type: str = "other",
               attachment_ids: list[str] = (),
               user_msg_id: str = "",
               context_photo=None,
               max_turns: "int | None" = 10,
               continue_run: bool = False,
               context_album=None,
               auth_header: str = "",
               user_lang: str = "",
               mcp_servers: list | None = None) -> RunSink:
    """Allocate a run row + sink and spawn the detached agent task. Returns
    the sink so the caller can immediately subscribe."""
    run_id = str(uuid.uuid4())
    now = int(time.time())
    _conn.execute(
        "INSERT INTO agent_runs (id, session_id, user_id, status, user_message, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (run_id, session_id, user_id, "running", message, now),
    )
    _conn.commit()

    sink = RunSink(run_id, session_id, _conn)
    _active_runs[session_id] = sink

    async def run_agent():
        error_msg: str | None = None
        cancelled = False
        try:
            await _runner.run(
                session_id, user_id, message, sink,
                provider_key, provider_url, model,
                kind=kind, chat_username=chat_username,
                user_patterns=user_patterns or [],
                run_id=run_id,
                provider_type=provider_type,
                thinking=thinking,
                attachment_ids=attachment_ids,
                context_photo=context_photo,
                max_turns=max_turns,
                continue_run=continue_run,
                context_album=context_album,
                auth_header=auth_header,
                user_lang=user_lang,
                mcp_servers=mcp_servers,
            )
        except asyncio.CancelledError:
            # User clicked stop, or session was cancelled. Surface a clean
            # termination so the frontend can render it and the next /run
            # isn't blocked by a busy lock. Re-raising would skip our own
            # finally cleanup.
            cancelled = True
            error_msg = "cancelled"
            try:
                await sink.put({"type": "error", "content": "已停止"})
                await sink.put({"type": "done"})
            except asyncio.CancelledError:
                pass
        except RuntimeError as e:
            error_msg = "agent_busy" if "agent_busy" in str(e) else str(e)
            if error_msg == "agent_busy":
                await sink.put({"type": "error", "content": "Agent is processing a previous message. Please wait."})
            else:
                await sink.put({"type": "error", "content": error_msg})
            await sink.put({"type": "done"})
        except Exception as e:
            error_msg = str(e)
            await sink.put({"type": "error", "content": error_msg})
            await sink.put({"type": "done"})
        finally:
            try:
                _conn.execute(
                    "UPDATE agent_runs SET status=?, error=?, finished_at=? WHERE id=?",
                    ("error" if error_msg else "done", error_msg, int(time.time()), run_id),
                )
                _conn.commit()
            except Exception:
                pass
            try:
                import memory_extract
                memory_extract.maybe_enqueue_extract_job(
                    _conn, session_id, user_id,
                    provider_url=provider_url, provider_key=provider_key,
                    provider_type=provider_type, model_name=model)
            except Exception:
                pass
            # Don't leave references to a finished task pinned in _active_runs;
            # the sink is still kept (so very-late subscribers can replay) but
            # the task ref is cleared so GC can reclaim its frames.
            sink.task = None
        # Suppress CancelledError so it doesn't trigger asyncio's
        # 'task exception was never retrieved' warnings; the sink already
        # emitted error+done, which is the user-visible signal we want.
        _ = cancelled

    task = asyncio.create_task(run_agent())
    sink.task = task
    return sink


@app.post("/agent/sessions/{session_id}/run")
async def run_session(
    session_id: str,
    req: RunRequest,
    request: Request,
    x_user_id: str = Header(..., alias="X-User-Id"),
    x_user_name: str = Header("", alias="X-User-Name"),
    x_agent_provider_key: str = Header(..., alias="X-Agent-Provider-Key"),
    x_agent_provider_url: str = Header(..., alias="X-Agent-Provider-Url"),
):
    _assert_owns_session(session_id, x_user_id)
    if not (req.model or "").strip():
        # 没有有效模型名:明确报错,而不是悄悄用某个默认名去打后端(会得到迷惑性的 404)。
        raise HTTPException(status_code=400,
                            detail="no model selected — pick a model before sending")
    if req.kind == "init" and _session_agent_type(session_id) != "general":
        # kind=init generates agent.md for a directory — path-layer only.
        raise HTTPException(status_code=400,
                            detail="kind=init not supported for this agent profile")

    # Validate attachment_ids belong to this session and are unbound
    if req.attachment_ids:
        placeholders = ",".join(["?"] * len(req.attachment_ids))
        rows = _conn.execute(
            f"SELECT id, message_id FROM attachments "
            f"WHERE id IN ({placeholders}) AND session_id = ?",
            (*req.attachment_ids, session_id),
        ).fetchall()
        found = {r["id"]: r["message_id"] for r in rows}
        if len(found) != len(req.attachment_ids):
            missing = [a for a in req.attachment_ids if a not in found]
            raise HTTPException(status_code=422,
                                detail=f"attachment not in session: {missing}")
        bound = [a for a, mid in found.items() if mid is not None]
        if bound:
            raise HTTPException(status_code=422,
                                detail=f"attachment already used: {bound}")

    if req.kind == "init":
        if not req.init_target:
            raise HTTPException(status_code=400,
                                detail="init_target required when kind=init")
        target = os.path.abspath(req.init_target)
        if not os.path.isdir(target):
            raise HTTPException(status_code=404, detail="init_target not a directory")
        _conn.execute(
            "INSERT INTO visible_resources (session_id, path, kind, added_at) "
            "VALUES (?,?,?,?) ON CONFLICT(session_id, path) DO NOTHING",
            (session_id, target, "folder", int(time.time())),
        )
        _conn.commit()

    # Reject overlapping runs on the same session. The agent.py lock would
    # also catch this, but rejecting here means we don't allocate an empty
    # run row for the rejected request.
    existing = _active_runs.get(session_id)
    if existing is not None and not existing.is_done:
        raise HTTPException(status_code=409, detail="agent_busy")

    user_patterns = _user_patterns_from_header(request)
    # The gateway's reverse proxy forwards the user's Authorization header
    # verbatim; photo tools need it for the Photos service's album endpoints.
    auth_header = request.headers.get("Authorization", "")
    # UI locale (e.g. "zh_cn") sent by the frontend; threaded into the system
    # prompt so the model replies in the user's language instead of guessing.
    user_lang = request.headers.get("Language", "").strip()

    # Resolve thinking config: request body → session row → user_settings default.
    from provider_adapters import ThinkingConfig, ThinkingLevel
    thinking_cfg = None
    if req.thinking is not None:
        thinking_cfg = ThinkingConfig(
            enabled=req.thinking.enabled,
            level=req.thinking.level,
        )
    else:
        row = _conn.execute(
            "SELECT thinking_enabled, thinking_level FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        if row and row["thinking_enabled"] is not None and row["thinking_level"]:
            thinking_cfg = ThinkingConfig(
                enabled=bool(row["thinking_enabled"]),
                level=ThinkingLevel(row["thinking_level"]),
            )
        else:
            # Fall back to user-level defaults; if missing, hard-code.
            thinking_cfg = _read_user_thinking_defaults(_conn, x_user_id)

    provider_type = request.headers.get("X-Agent-Provider-Type", "other")

    mt_raw = _read_max_turns_setting(_conn, x_user_id)
    max_turns = None if mt_raw == 0 else mt_raw

    # Inject SKILL.md into the message when X-Skill-Id header is present.
    # Fix 1.2: validate skill_id via regex BEFORE any path operations.
    skill_id = request.headers.get("X-Skill-Id", "").strip()
    if skill_id:
        if not _SKILL_ID_RE.match(skill_id):
            # Refuse silently — malformed id would have been a 400 in the Go layer.
            skill_id = ""
    if skill_id:
        skills_root = os.environ.get("NIMOOS_SKILLS_ROOT", "/var/lib/nimoos/ai/skills")
        bundle = os.path.join(skills_root, ".runtime", str(x_user_id), skill_id)
        md_path = os.path.join(bundle, "SKILL.md")
        # Belt + suspenders: even after regex, double-check resolved real path
        # stays inside the runtime view (defense against symlink games).
        try:
            real_md = os.path.realpath(md_path)
            real_root = os.path.realpath(skills_root)
            if not real_md.startswith(real_root + os.sep):
                md_path = None
        except OSError:
            md_path = None
        if md_path and os.path.isfile(md_path):
            size = os.path.getsize(md_path)
            if size <= _MAX_SKILL_MD_BYTES:
                with open(md_path) as f:
                    md = f.read()
                req = req.model_copy(update={
                    "message": f"(Using skill `{skill_id}`. SKILL.md follows.)\n\n{md}\n\n---\n\n{req.message}"
                })

    user_msg_id = "msg_" + uuid.uuid4().hex[:12]
    if req.attachment_ids:
        placeholders = ",".join(["?"] * len(req.attachment_ids))
        _conn.execute(
            f"UPDATE attachments SET message_id = ? "
            f"WHERE id IN ({placeholders}) AND session_id = ?",
            (user_msg_id, *req.attachment_ids, session_id),
        )
        _conn.commit()

    from mcp_client.runtime import fetch_mcp_servers
    mcp_servers = await fetch_mcp_servers(request.headers.get("X-Agent-MCP-Ticket", ""))

    sink = _start_run(
        session_id, x_user_id, req.message,
        x_agent_provider_key, x_agent_provider_url, req.model,
        kind=req.kind, chat_username=x_user_name,
        user_patterns=user_patterns,
        thinking=thinking_cfg,
        provider_type=provider_type,
        attachment_ids=req.attachment_ids,
        user_msg_id=user_msg_id,
        context_photo=req.context_photo,
        max_turns=max_turns,
        continue_run=req.continue_run,
        context_album=req.context_album,
        auth_header=auth_header,
        user_lang=user_lang,
        mcp_servers=mcp_servers,
    )
    return StreamingResponse(_stream_from_sink(sink), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "Connection": "keep-alive",
                                      "X-Accel-Buffering": "no"})


@app.get("/agent/sessions/{session_id}/run-stream")
async def stream_active_run(
    session_id: str,
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    """Reattach to the latest run for this session.

    Use this on page load / tab reopen to pick up a run that started in a
    previous browser session. Replays everything from the event log, then
    live-tails any new events until the run finishes.

    Returns 204 (no body) when the session has never had a run, so the UI
    can short-circuit without parsing an empty SSE stream.
    """
    row = _conn.execute("SELECT id FROM sessions WHERE id=? AND user_id=?",
                        (session_id, x_user_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="session not found")

    # Three cases the resume endpoint serves:
    #   1. Live sink, not yet done — stream with prefix (live tail).
    #   2. Sink/DB run in 'error' state — _save_history never ran, /messages
    #      misses this turn, so we replay from the event log to surface it.
    #   3. Sink/DB run in 'done' state — /messages already has it; 204.
    sse_headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }

    sink = _active_runs.get(session_id)
    if sink is not None and not sink.is_done:
        run_row = _conn.execute(
            "SELECT user_message FROM agent_runs WHERE id=?",
            (sink.run_id,),
        ).fetchone()
        prefix = []
        if run_row and run_row["user_message"]:
            prefix.append({"type": "user_message", "content": run_row["user_message"]})
        return StreamingResponse(_stream_from_sink(sink, prefix=prefix),
                                 media_type="text/event-stream", headers=sse_headers)

    latest = _conn.execute(
        "SELECT id, user_message, status FROM agent_runs "
        "WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if not latest:
        return JSONResponse(status_code=204, content=None)

    if latest["status"] == "done":
        # _save_history persisted this turn into messages, /messages will
        # render it. Nothing for the resume stream to do.
        return JSONResponse(status_code=204, content=None)

    # status='error' (or anything non-'done' that slipped through). Replay
    # whatever the event log captured so the user sees their interrupted turn.
    events = load_events_from_db(_conn, latest["id"])
    prefix = []
    if latest["user_message"]:
        prefix.append({"type": "user_message", "content": latest["user_message"]})
    return StreamingResponse(_stream_replay_only(events, prefix=prefix),
                             media_type="text/event-stream", headers=sse_headers)


@app.post("/agent/sessions/{session_id}/confirm")
async def confirm_session(
    session_id: str,
    request: Request,
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    confirmed = True
    remember = False
    confirm_id = ""
    body = await request.body()
    if body:
        try:
            import json as _json
            data = _json.loads(body)
            confirmed = bool(data.get("confirmed", True))
            remember = bool(data.get("remember", False))
            confirm_id = str(data.get("confirm_id") or "")
        except Exception:
            pass
    if not confirm_id:
        raise HTTPException(status_code=400, detail="confirm_id_required")
    try:
        _confirm_mgr.resolve(confirm_id, confirmed, remember=remember,
                             expected_session_id=session_id)
    except KeyError as e:
        # confirm_expired (id unknown / already resolved / agent restarted) or
        # confirm_session_mismatch (id belongs to another session). Both are 409.
        raise HTTPException(status_code=409, detail=str(e.args[0]) if e.args else "confirm_expired")
    return {"ok": True}


@app.post("/agent/sandbox-run")
async def sandbox_run_endpoint(
    req: SandboxRunRequest,
    request: Request,
    x_user_id: str = Header(..., alias="X-User-Id"),
    x_user_name: str = Header("", alias="X-User-Name"),
    x_agent_provider_key: str = Header(..., alias="X-Agent-Provider-Key"),
    x_agent_provider_url: str = Header(..., alias="X-Agent-Provider-Url"),
):
    """Run a skill in an isolated, no-DB sandbox session."""
    import re
    if not re.match(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$", req.skill_id):
        raise HTTPException(status_code=400, detail="invalid skill_id")
    skills_root = os.environ.get("NIMOOS_SKILLS_ROOT", "/var/lib/nimoos/ai/skills")
    bundle_dir = None
    for candidate in (
        os.path.join(skills_root, "builtin", req.skill_id),
        os.path.join(skills_root, "users", x_user_id, req.skill_id),
    ):
        if os.path.isdir(candidate):
            bundle_dir = candidate
            break
    if bundle_dir is None:
        raise HTTPException(status_code=404, detail="skill not found")

    sink = RunSink("sandbox-" + uuid.uuid4().hex[:8], "sandbox", _conn)
    from sandbox_run import run_sandbox

    async def runner_task():
        await run_sandbox(
            runner=_runner,
            bundle_dir=bundle_dir,
            user_prompt=req.prompt,
            user_id=x_user_id,
            skills_root=skills_root,
            provider_key=x_agent_provider_key,
            provider_url=x_agent_provider_url,
            model="",  # provider default
            provider_type=request.headers.get("X-Agent-Provider-Type", "other"),
            sink=sink,
        )

    asyncio.create_task(runner_task())
    return StreamingResponse(
        _stream_from_sink(sink),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/agent/sessions/{session_id}/cancel")
async def cancel_session(
    session_id: str,
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    # 1) Reject every in-flight confirmation for this session.
    confirms_cancelled = _confirm_mgr.cancel_session(session_id)
    # 2) Cancel the agent task itself if it's still running. Without this,
    #    the per-session lock in agent.py stays held and the next /run is
    #    rejected with agent_busy. The wrapper around _runner.run catches
    #    CancelledError and emits a clean error+done pair.
    #
    # We AWAIT the task's completion before returning so that when the
    # client's stop POST resolves, the lock is guaranteed released and an
    # immediate follow-up /run won't race into agent_busy.
    task_cancelled = False
    sink = _active_runs.get(session_id)
    if sink is not None and sink.task is not None and not sink.task.done():
        sink.task.cancel()
        task_cancelled = True
        try:
            await asyncio.wait_for(sink.task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            # We don't care why the task ended — only that the lock is freed.
            pass
    return {"ok": True, "confirms_cancelled": confirms_cancelled, "task_cancelled": task_cancelled}


@app.post("/agent/mcp/test")
async def mcp_test(request: Request):
    import mcp_client.client as mcp_client
    cfg = await request.json()
    return await mcp_client.test_server(cfg)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8282)
