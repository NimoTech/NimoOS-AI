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

# The container starts this file with `python main.py`, so it executes as
# `__main__`. Anything that reaches back into it by name — `tasks/runner.py`,
# `tasks/notify.py` — does `import main`, which would otherwise execute this
# file a SECOND time and hand out a DIFFERENT module object: a second
# AgentRunner, a second sqlite connection, a second ConfirmManager.
#
# That split silently broke every scheduled task that needed the network
# (found on 118, 2026-08-17): a task run registered its sink on
# `main._runner._active_sinks`, while `/internal/egress-confirm` — served by
# the app in `__main__` — looked in `__main__._runner._active_sinks`, found
# nothing, and fail-closed. The egress-proxy then answered 403 "blocked by
# policy" for every outbound connection an unattended run made, no matter what
# `preauth.egress_domains` allowed. Interactive chats and channels were
# unaffected because they never import this module by name.
#
# Aliasing the two names makes `import main` return this very module. Safe
# because every `import main` in the codebase is deferred inside a function
# (never at module import time), so nobody observes a half-initialized module.
if __name__ == "__main__":
    sys.modules.setdefault("main", sys.modules["__main__"])

_SKILL_ID_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")
_MAX_SKILL_MD_BYTES = 50 * 1024

from fastapi import Body, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, Response, StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

import agent_md
import db as db_module
import context_compaction
import mcp_tokens
import memory_store
from agent import AgentRunner
import confirm as _confirm_mod
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
# Timeout (seconds) for the egress-proxy's own -confirm-timeout, i.e. how long
# the Go proxy's HTTP client will wait on this route before it fail-closes the
# CONNECT (see egress-proxy/main.go's confirmClient doc for the 2026-08-16
# incident this fixes: proxy defaulted to 5s, Python defaulted to 24h, so no
# human could ever click the card in time). Keep EGRESS_CONFIRM_TIMEOUT a bit
# ABOVE the 110s passed to mgr.wait() below, so Python resolves (and can log a
# clean "timed out, denied") before the proxy's own HTTP call times out.
EGRESS_CONFIRM_TIMEOUT = _env_int("NIMOOS_EGRESS_CONFIRM_TIMEOUT", 120)

# Subprocess handles for orchestrated children (executor + proxy).
# Populated by the startup handler; used by the shutdown handler.
_executor_proc: "subprocess.Popen | None" = None
_proxy_proc: "subprocess.Popen | None" = None

app = FastAPI(title="nimoos-agent")


# container-local liveness probe — intentionally minimal (no auth/DB)
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


import contextlib
from starlette.routing import Route as _StarletteRoute
from mcp_server import server as _mcp_server

_mcp_asgi, _mcp_session_mgr = _mcp_server.build(_conn)
_mcp_exit_stack = contextlib.AsyncExitStack()

# Mount the MCP Streamable-HTTP ASGI app so that BOTH the bare path /mcp-rpc
# AND the trailing-slash form /mcp-rpc/ (and any subpaths) dispatch directly
# to the app without a 307 redirect.
#
# Problem: app.mount("/mcp-rpc", _mcp_asgi) creates a Starlette Mount whose
# regex only matches /mcp-rpc/… (with leading slash after the prefix).  When
# the bare /mcp-rpc is requested the Mount returns Match.NONE; the Router then
# runs its redirect_slashes logic, finds a match for /mcp-rpc/, and emits a
# 307.  External MCP clients configured with the bare URL break because the
# redirect Location drops the /v1/ai prefix that the Go gateway stripped.
#
# Fix: add an explicit Starlette Route for the exact path /mcp-rpc that wraps
# the ASGI callable in a trivial class (Route treats a class instance as a raw
# ASGI app, not as a request→response function).  This route wins the FULL
# match before the Router ever reaches its redirect_slashes branch.  The Mount
# keeps handling /mcp-rpc/ and all subpaths.

class _McpRpcBare:
    """Wrap _mcp_asgi as a class so Starlette's Route treats it as a raw ASGI
    app (not a request-response function) when registered with methods=None."""
    async def __call__(self, scope, receive, send):
        await _mcp_asgi(scope, receive, send)

app.router.routes.insert(0, _StarletteRoute("/mcp-rpc", endpoint=_McpRpcBare(), methods=None))
app.mount("/mcp-rpc", _mcp_asgi)  # handles /mcp-rpc/ and /mcp-rpc/{rest:path}


@app.on_event("startup")
async def _mcp_startup():
    await _mcp_exit_stack.enter_async_context(_mcp_session_mgr.run())


@app.on_event("shutdown")
async def _mcp_shutdown():
    await _mcp_exit_stack.aclose()


from fastapi import Body, Header
from fastapi.responses import JSONResponse


def _require_uid(x_user: str | None) -> str:
    if not x_user:
        raise HTTPException(status_code=401, detail="missing user identity")
    return x_user


@app.post("/mcp-tokens")
async def mcp_token_create(payload: dict = Body(default={}),
                           x_nimoos_user_id: str | None = Header(default=None)):
    uid = _require_uid(x_nimoos_user_id)
    tok_id, token = mcp_tokens.create(
        _conn, uid, str(payload.get("label", "")), now_ms=int(time.time() * 1000))
    return JSONResponse(status_code=201,
                        content={"id": tok_id, "token": token,
                                 "label": payload.get("label", "")})


@app.get("/mcp-tokens")
async def mcp_token_list(x_nimoos_user_id: str | None = Header(default=None)):
    uid = _require_uid(x_nimoos_user_id)
    return {"tokens": mcp_tokens.list_for_user(_conn, uid)}


@app.delete("/mcp-tokens/{token_id}")
async def mcp_token_delete(token_id: str,
                           x_nimoos_user_id: str | None = Header(default=None)):
    uid = _require_uid(x_nimoos_user_id)
    return {"revoked": mcp_tokens.revoke(_conn, uid, token_id)}


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
async def _tracing_startup():
    """Install tracing and sync the enable flag from the global setting."""
    try:
        import phoenix_tracing
        phoenix_tracing.setup_tracing()
        phoenix_tracing.refresh_enabled_flag(_db())
    except Exception:
        _LOG.warning("tracing startup failed; continuing", exc_info=True)


@app.on_event("startup")
async def _memory_worker_startup():
    import memory_extract
    memory_extract.start_worker(_db())


@app.on_event("startup")
async def _recall_worker_startup():
    import recall_index
    recall_index.start_worker(_db())

    from notes.sync import start_notes_sync
    _notes_sync_task, _notes_sync_stop = start_notes_sync(_db())


@app.on_event("startup")
async def _notes_extract_worker_startup():
    import notes_extract
    notes_extract.start_worker(_db())


@app.on_event("startup")
async def _notes_distill_worker_startup():
    import notes_distill
    notes_distill.start_worker(_db())


@app.on_event("startup")
async def _notes_distill_scanner_startup():
    import notes_distill_scan
    notes_distill_scan.start_scanner(_db())


# Kept alive for the process's lifetime: an asyncio.Task with no strong
# reference anywhere can be garbage-collected mid-await.
_tasks_workers = {}


@app.on_event("startup")
async def _tasks_runner_startup():
    """Scheduled-task run worker.

    Registered BEFORE the scheduler on purpose: start_worker() reconciles runs
    left 'queued'/'running' by the previous process (marking them failed, never
    replaying them), and doing that after the scheduler's first tick would
    kill runs it had just legitimately enqueued.
    """
    try:
        from tasks import runner as tasks_runner
        _tasks_workers["runner"] = tasks_runner.start_worker(_db())
    except Exception:
        _LOG.exception("tasks runner startup failed; scheduled tasks will not run")


@app.on_event("startup")
async def _tasks_scheduler_startup():
    try:
        from tasks import scheduler as tasks_scheduler
        _tasks_workers["scheduler"] = tasks_scheduler.start_worker(_db())
    except Exception:
        _LOG.exception("tasks scheduler startup failed; scheduled tasks will not fire")


_channel_manager = None


def _channel_start_run(session_id: str, user_id: str, message: str,
                       creds: dict, chat_username: str = "",
                       *, attachment_ids: list[str] = (),
                       channel_send_file=None):
    """Channel-side bridge into _start_run. Credentials come pre-resolved
    from the Go internal endpoint instead of X-Agent-Provider-* headers."""
    return _start_run(
        session_id, user_id, message,
        creds["api_key"], creds["base_url"], creds["model"],
        provider_type=creds.get("provider_type", "other"),
        chat_username=chat_username,
        attachment_ids=attachment_ids,
        channel_send_file=channel_send_file,
    )


async def _channel_cancel_run(session_id: str) -> bool:
    """Mirror of cancel_session's task-cancel path, for channel /stop."""
    sink = _active_runs.get(session_id)
    if sink is None or sink.task is None or sink.task.done():
        return False
    sink.task.cancel()
    try:
        await asyncio.wait_for(sink.task, timeout=5.0)
    except Exception:
        pass
    return True


@app.on_event("startup")
async def _channels_startup():
    global _channel_manager
    try:
        from channels import credentials as channel_credentials
        from channels.manager import ChannelManager
        from channels.router import ChannelRouter
        router = ChannelRouter(
            _conn,
            start_run=_channel_start_run,
            cancel_run=_channel_cancel_run,
            resolve_credentials=channel_credentials.resolve,
            resolve_confirm=_confirm_mgr.resolve,
        )
        _channel_manager = ChannelManager(_conn, router)
        await _channel_manager.start_all()
    except Exception:
        _LOG.exception("channels startup failed; continuing without channels")


@app.on_event("shutdown")
async def _channels_shutdown():
    if _channel_manager is not None:
        await _channel_manager.stop_all()


class ChannelInstanceCreate(BaseModel):
    channel_type: str
    name: str = ""
    config: dict = Field(default_factory=dict)


class ChannelInstanceUpdate(BaseModel):
    enabled: bool


class PairingCodeRequest(BaseModel):
    instance_id: str


class BindingModelUpdate(BaseModel):
    model: str


class BindingDownloadDirUpdate(BaseModel):
    download_dir: str


def _default_download_dir(channel_type: str) -> str:
    return f"/DATA/Downloads/{channel_type}"


def _validate_data_subdir(path: str) -> str | None:
    """Return the abspath if it is inside /DATA and not under .system_data;
    else None. (Channel download dirs must live in the user-visible /DATA.)"""
    ap = os.path.abspath(path)
    if ap != "/DATA" and not ap.startswith("/DATA/"):
        return None
    if "/.system_data" in ap + "/":
        return None
    return ap


def _mask_instance(row: dict) -> dict:
    cfg = json.loads(row["config_json"])
    token = cfg.get("bot_token", "")
    out = {"id": row["id"], "channel_type": row["channel_type"],
           "name": row["name"], "enabled": bool(row["enabled"]),
           "bot_username": cfg.get("bot_username", ""),
           "token_tail": token[-4:] if token else "",
           "created_at": row["created_at"]}
    if row["channel_type"] == "discord" and cfg.get("application_id"):
        # Bots can only DM users who share a server; the admin needs this
        # invite link to add the bot to a server before anyone can pair.
        # permissions=274877991936 = View Channels + Send Messages +
        # Read Message History (minimal for DM-based use).
        out["invite_url"] = (
            "https://discord.com/oauth2/authorize?client_id="
            + str(cfg["application_id"])
            + "&scope=bot&permissions=274877991936")
    return out


@app.post("/agent/channels/instances")
async def channel_instance_create(
        body: ChannelInstanceCreate,
        x_user_id: str = Header(..., alias="X-User-Id")):
    from channels import store as channel_store
    from channels.manager import ADAPTERS
    adapter_cls = ADAPTERS.get(body.channel_type)
    # This endpoint creates an instance out of a bot token, so it only accepts
    # adapters that can vet one. Membership in ADAPTERS is NOT the right gate:
    # ADAPTERS is the manager's runtime registry, and "lark" lives there so the
    # manager can run the instance even though its credentials come from
    # lark-cli, not from a token — it is created through /agent/channels/lark.
    # Gating on the capability (rather than on the string "lark") means the
    # next credential-less adapter is rejected with a 422 here instead of
    # 500-ing on a missing `validate_token` attribute.
    if adapter_cls is None or not hasattr(adapter_cls, "validate_token"):
        raise HTTPException(status_code=422, detail="unsupported channel_type")
    config = dict(body.config)
    token = (config.get("bot_token") or "").strip()
    if not token:
        raise HTTPException(status_code=422, detail="bot_token required")
    info = await adapter_cls.validate_token(token)
    if info is None:
        raise HTTPException(status_code=422, detail="bot token rejected")
    config["bot_token"] = token
    config.update(info)
    row = channel_store.create_instance(
        _conn, body.channel_type, body.name, config, x_user_id,
        now_ms=int(time.time() * 1000))
    if _channel_manager is not None:
        await _channel_manager.reload()
    return JSONResponse(status_code=201, content=_mask_instance(row))


@app.get("/agent/channels/instances")
async def channel_instance_list(
        x_user_id: str = Header(..., alias="X-User-Id")):
    from channels import store as channel_store
    return {"instances": [_mask_instance(r)
                          for r in channel_store.list_instances(_conn)]}


@app.get("/agent/channels/pairable-instances")
async def channel_pairable_instances(
        x_user_id: str = Header(..., alias="X-User-Id")):
    """Enabled channel instances any NimoOS user can pair their own account
    with. Unlike /instances (the admin bot-config view, which exposes the
    token tail), this is a minimal, token-free list available to every user:
    the bot serves every NimoOS user and each pairs their own account."""
    from channels import store as channel_store
    out = []
    for r in channel_store.list_instances(_conn):
        if not r["enabled"]:
            continue
        if r["channel_type"] == "lark":
            # No inbound path ever redeems a pairing code against Lark: this
            # milestone consumes only card clicks, never messages. Offering
            # it here would advertise a pairing flow that can never complete.
            continue
        cfg = json.loads(r["config_json"])
        item = {"id": r["id"], "channel_type": r["channel_type"],
                "name": r["name"], "bot_username": cfg.get("bot_username", "")}
        if r["channel_type"] == "discord" and cfg.get("application_id"):
            item["invite_url"] = (
                "https://discord.com/oauth2/authorize?client_id="
                + str(cfg["application_id"])
                + "&scope=bot&permissions=274877991936")
        out.append(item)
    return {"instances": out}


@app.put("/agent/channels/instances/{iid}")
async def channel_instance_update(
        iid: str, body: ChannelInstanceUpdate,
        x_user_id: str = Header(..., alias="X-User-Id")):
    from channels import store as channel_store
    ok = channel_store.set_instance_enabled(
        _conn, iid, body.enabled, now_ms=int(time.time() * 1000))
    if not ok:
        raise HTTPException(status_code=404, detail="instance not found")
    if _channel_manager is not None:
        await _channel_manager.reload()
    return {"ok": True}


@app.delete("/agent/channels/instances/{iid}")
async def channel_instance_delete(
        iid: str, x_user_id: str = Header(..., alias="X-User-Id")):
    from channels import store as channel_store
    ok = channel_store.delete_instance(_conn, iid)
    if _channel_manager is not None:
        await _channel_manager.reload()
    return {"ok": ok}


@app.post("/agent/channels/pairing-code")
async def channel_pairing_code(
        body: PairingCodeRequest,
        x_user_id: str = Header(..., alias="X-User-Id")):
    from channels import store as channel_store
    inst = channel_store.get_instance(_conn, body.instance_id)
    if inst is None or not inst["enabled"]:
        raise HTTPException(status_code=404, detail="instance not found")
    code, expires = channel_store.create_pairing_code(
        _conn, body.instance_id, x_user_id, now_ms=int(time.time() * 1000))
    return JSONResponse(status_code=201,
                        content={"code": code, "expires_at": expires})


@app.get("/agent/channels/bindings")
async def channel_binding_list(
        x_user_id: str = Header(..., alias="X-User-Id")):
    from channels import store as channel_store
    out = []
    for b in channel_store.list_bindings_for_user(_conn, x_user_id):
        inst = channel_store.get_instance(_conn, b["instance_id"]) or {}
        out.append({"id": b["id"], "instance_id": b["instance_id"],
                    "channel_type": inst.get("channel_type", ""),
                    "instance_name": inst.get("name", ""),
                    "external_username": b["external_username"],
                    "external_user_id": b["external_user_id"],
                    "default_model": b["default_model"],
                    "download_dir": b["download_dir"] or _default_download_dir(
                        inst.get("channel_type", "")),
                    "created_at": b["created_at"]})
    return {"bindings": out}


@app.delete("/agent/channels/bindings/{bid}")
async def channel_binding_delete(
        bid: str, x_user_id: str = Header(..., alias="X-User-Id")):
    from channels import store as channel_store
    return {"revoked": channel_store.revoke_binding(_conn, x_user_id, bid)}


@app.put("/agent/channels/bindings/{bid}/model")
async def channel_binding_model(
        bid: str, body: BindingModelUpdate,
        x_user_id: str = Header(..., alias="X-User-Id")):
    from channels import store as channel_store
    ok = channel_store.set_binding_model(_conn, x_user_id, bid, body.model)
    if not ok:
        raise HTTPException(status_code=404, detail="binding not found")
    return {"ok": True}


@app.put("/agent/channels/bindings/{bid}/download-dir")
async def channel_binding_download_dir(
        bid: str, body: BindingDownloadDirUpdate,
        x_user_id: str = Header(..., alias="X-User-Id")):
    from channels import store as channel_store
    ap = _validate_data_subdir(body.download_dir)
    if ap is None:
        raise HTTPException(status_code=422,
                            detail="download_dir must be under /DATA (not .system_data)")
    ok = channel_store.set_binding_download_dir(_conn, x_user_id, bid, ap)
    if not ok:
        raise HTTPException(status_code=404, detail="binding not found")
    return {"ok": True}


def _lark_buttons_ready(instance_id: str) -> bool:
    """Runtime truth for the settings page: is the click consumer up?"""
    mgr = globals().get("_channel_manager")
    if mgr is None or not instance_id:
        return False
    entry = (getattr(mgr, "_running", None) or {}).get(instance_id)
    if entry is None:
        return False
    return bool(getattr(entry[0], "buttons_available", False))


async def _reload_channels() -> None:
    """Apply an instance change to the running adapters. Never fatal."""
    mgr = globals().get("_channel_manager")
    if mgr is None:
        return
    try:
        await mgr.reload()
    except Exception:
        _LOG.exception("lark channel: manager reload failed")


@app.get("/agent/channels/lark")
async def lark_channel_status(x_user_id: str = Header(..., alias="X-User-Id")):
    from channels import lark_setup
    st = lark_setup.status(_db(), x_user_id)
    st["buttons_ready"] = _lark_buttons_ready(st.get("instance_id") or "")
    return st


@app.post("/agent/channels/lark")
async def lark_channel_enable(x_user_id: str = Header(..., alias="X-User-Id")):
    from channels import lark_setup
    try:
        await lark_setup.enable(_db(), x_user_id, now_ms=int(time.time() * 1000))
    except lark_setup.LarkSetupError:
        # Not a 500: "lark-cli is not installed or not logged in" is the
        # DEFAULT state of a fresh box and the first thing the settings page
        # asks about. (The same 409 also covers a CLI that IS logged in but
        # only holds the bot identity — resolve_bot_identity cannot tell the
        # two apart without a Task 3 change, so the user-facing string names
        # both possibilities; see channelsLarkEnableFailed.)
        raise HTTPException(409, "lark_unavailable")
    await _reload_channels()
    st = lark_setup.status(_db(), x_user_id)
    st["buttons_ready"] = _lark_buttons_ready(st.get("instance_id") or "")
    return st


@app.delete("/agent/channels/lark", status_code=204)
async def lark_channel_disable(x_user_id: str = Header(..., alias="X-User-Id")):
    from channels import lark_setup
    lark_setup.disable(_db(), x_user_id)
    await _reload_channels()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Startup orchestration helpers (netns + egress-proxy + executor)
# ---------------------------------------------------------------------------

def _build_proxy_argv(
    proxy_bin: str,
    listen: str = "169.254.7.1:8888",
    dns: str = "169.254.7.1:53",
    confirm_url: str = "http://127.0.0.1:8282/internal/egress-confirm",
    grant_listen: str = "127.0.0.1:8889",
    confirm_timeout: int = EGRESS_CONFIRM_TIMEOUT,
) -> list[str]:
    """Return the argv list for starting the egress-proxy.

    Extracted as a pure function so tests can verify argv construction without
    actually spawning a process.

    NOTE (P0 design): The grant_listen address exists for A-path (content-
    inspection allow-listing) but main.py does not call it in this phase — there
    is no content judge, so the grant channel has no caller.  Proxy grant support
    is wired at the process level; P1 will add the judge that calls it.

    `confirm_timeout` defaults to EGRESS_CONFIRM_TIMEOUT (env-overridable via
    NIMOOS_EGRESS_CONFIRM_TIMEOUT, default 120s) and is passed through as the
    proxy's `-confirm-timeout` flag — see egress-proxy/main.go's confirmClient
    doc comment for why this must give a human real time to answer a
    confirmation card, and must stay a bit above the 110s the egress_confirm
    route below passes to ConfirmManager.wait().
    """
    return [
        proxy_bin,
        "-listen", listen,
        "-dns", dns,
        "-confirm-url", confirm_url,
        "-grant-listen", grant_listen,
        "-confirm-timeout", f"{confirm_timeout}s",
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

# The proxy's reason code for "first connection to a host nobody has confirmed
# yet" — the domain gate. Its sibling, "upload_over_threshold", is the upload
# DLP gate; see egress_confirm for why only the former can be auto-approved.
TOFU_UNKNOWN_HOST_REASON = "tofu_unknown_host"


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
    # A configured search backend must not raise a card on every TOFU miss:
    # the TTL is an hour, so an interactive user would be asked all day for a
    # host an administrator already chose. Checked BEFORE the session lookup
    # so an unattended run gets the same answer as an interactive one.
    #
    # Scoped to reason == "tofu_unknown_host" ON PURPOSE. The proxy calls this
    # one endpoint for two different controls, and the design authorises the
    # exemption for the domain gate only:
    #   * "tofu_unknown_host"     — first connection to an unknown host
    #   * "upload_over_threshold" — this connection pushed >64 KB outward
    # The second is the DLP control. Matching on host alone would silently
    # remove the upload confirmation for the configured provider — an injected
    # page could then have the agent call web_search with a local file's
    # contents as the query and exfiltrate it with no card raised — and the
    # proxy would additionally register a byte grant for later connections.
    #
    # This is not a gate bypass primitive: the sandbox can POST here (the
    # proxy's internal-target branch reaches container loopback), but the
    # response only carries a verdict — markConfirmed happens inside the Go
    # proxy after it receives `true`, so there is no state here to write.
    try:
        from web import settings as _web_settings  # noqa: PLC0415
        _preapproved = _web_settings.preapproved_hosts(
            _web_settings.load(_db()))
    except Exception:  # noqa: BLE001 — a config read must never gate egress open
        _preapproved = set()
    if req.reason == TOFU_UNKNOWN_HOST_REASON and req.host.lower() in _preapproved:
        from audit import audit as _audit  # noqa: PLC0415
        _audit("egress_grant", host=req.host, bytes=req.bytes,
               reason=req.reason, decision="auto_approved_search_backend")
        return {"allow": True}

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
        # 110s, NOT the ConfirmManager 24h default: this call is answering a
        # synchronous HTTP POST from the Go egress-proxy, which itself gives up
        # after -confirm-timeout (default 120s — see EGRESS_CONFIRM_TIMEOUT /
        # egress-proxy/main.go's confirmClient doc for the incident this fixes).
        # 110 < 120 so THIS wait times out first and we can log/attribute a
        # clean "timed out, denied" instead of the proxy just seeing its own
        # HTTP client deadline expire with no explanation.
        granted = await _confirm_mgr.wait(cid, timeout=110)
        from audit import audit as _audit  # noqa: PLC0415
        _audit("egress_grant", session_id=session_id, host=req.host,
               bytes=req.bytes, reason=req.reason,
               decision="approved" if granted else "denied")
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


class TracingSettingPayload(BaseModel):
    enabled: bool


class MemorySettingsPayload(BaseModel):
    enabled: bool
    compaction_enabled: "bool | None" = None
    context_window: "int | None" = None


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
    out = []
    for r in rows:
        d = dict(r)
        if d.get("kind") == "folder":
            # Evaluated fresh on every listing: directory permissions can
            # change after authorization, so a persisted verdict would be
            # stale — and stale is not acceptable for a security decision.
            st = agent_md.probe(d["path"], read_body=False)
            d["agent_md"] = {"state": st.state, "reason": st.reason,
                             "detail": st.detail}
        out.append(d)
    return out


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
    # source != 'task': every scheduled run opens its own session with a NULL
    # title, and they sort by updated_at like any other — so a task firing
    # every 5 minutes would push the user's real conversations off the top of
    # the chat list. Task sessions are reachable through the run history,
    # which is where they belong. `source` is NOT NULL DEFAULT 'web', so the
    # comparison never drops a legacy row to a NULL result.
    rows = _db().execute(
        "SELECT id, title, created_at, updated_at, agent_type "
        "FROM sessions WHERE user_id=? AND source != 'task' "
        "ORDER BY updated_at DESC",
        (x_user_id,)
    ).fetchall()
    return [dict(row) for row in rows]


@app.delete("/agent/sessions/{session_id}")
async def delete_session(session_id: str, x_user_id: str = Header(..., alias="X-User-Id")):
    # Every step (vector cleanup, the child tables that have no FK cascade,
    # the snapshot dir) lives in session_purge, shared with the scheduled-task
    # runner's prune path — see that module for why the order is fixed. This
    # handler used to carry its own copy, which cleared neither `agent_runs`
    # nor `event_log`.
    import session_purge
    await session_purge.purge_session(_conn, x_user_id, session_id,
                                      snapshots_root=_snapshots_root)
    return {"ok": True}


@app.get("/agent/sessions/{session_id}/messages")
async def list_messages(session_id: str, x_user_id: str = Header(..., alias="X-User-Id")):
    row = _conn.execute("SELECT id FROM sessions WHERE id=? AND user_id=?",
                        (session_id, x_user_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="session not found")

    last_row = _conn.execute(
        "SELECT content FROM messages WHERE session_id=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
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
        "SELECT confirm_id, run_id, path, kind, reason, reason_key, decision "
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
            "reasonKey": r["reason_key"] or "",
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
        "SELECT content FROM messages WHERE session_id=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
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


@app.get("/agent/user-settings/tracing")
async def get_tracing_setting(request: Request):
    import phoenix_tracing
    return {"enabled": phoenix_tracing.tracing_globally_enabled(_db())}


@app.put("/agent/user-settings/tracing")
async def put_tracing_setting(request: Request, body: TracingSettingPayload):
    import phoenix_tracing
    phoenix_tracing.set_tracing_globally_enabled(_db(), body.enabled)
    return {"enabled": body.enabled}


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
    return {
        "enabled": memory_store.is_memory_enabled(_db(), user_id),
        "compaction_enabled": memory_store.is_compaction_enabled(_db(), user_id),
        "context_window": memory_store.get_context_window(_db(), user_id),
    }


@app.get("/agent/context-usage")
async def get_context_usage(request: Request):
    user_id = request.headers.get("X-User-Id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="X-User-Id required")
    session_id = request.query_params.get("session_id", "")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    model = request.query_params.get("model", "")
    return context_compaction.compute_usage(
        _db(), session_id=session_id, user_id=user_id, model=model)


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
    if body.compaction_enabled is not None:
        conn.execute(
            "INSERT INTO user_settings(user_id, key, value, updated_at) "
            "VALUES(?, 'compaction_enabled', ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (user_id, "1" if body.compaction_enabled else "0", int(time.time())),
        )
    if body.context_window is not None:
        val = body.context_window if (
            isinstance(body.context_window, int) and body.context_window > 0
        ) else ""
        conn.execute(
            "INSERT INTO user_settings(user_id, key, value, updated_at) "
            "VALUES(?, 'context_window', ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (user_id, str(val), int(time.time())),
        )
    conn.commit()
    return {
        "enabled": body.enabled,
        "compaction_enabled": memory_store.is_compaction_enabled(conn, user_id),
        "context_window": memory_store.get_context_window(conn, user_id),
    }


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
               mcp_servers: "list | ConfigUnavailable | None" = None,
               channel_send_file=None,
               pre_confirmed_tools: "set[str] | None" = None,
               run_shell_allowlist: "list | None" = None,
               run_scripts: "list | None" = None) -> RunSink:
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
                channel_send_file=channel_send_file,
                # Run-scoped pre-authorization (scheduled tasks); None for
                # every interactive path, which keeps the gates unchanged.
                pre_confirmed_tools=pre_confirmed_tools,
                run_shell_allowlist=run_shell_allowlist,
                run_scripts=run_scripts,
            )
        except asyncio.CancelledError:
            # User clicked stop, or session was cancelled. Surface a clean
            # termination so the frontend can render it and the next /run
            # isn't blocked by a busy lock. Re-raising would skip our own
            # finally cleanup.
            cancelled = True
            error_msg = "cancelled"
            try:
                await sink.put({"type": "error", "content": "Stopped"})
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
            try:
                import recall_index
                recall_index.maybe_enqueue_index_job(_conn, session_id, user_id)
            except Exception:
                pass
            try:
                import notes_extract
                notes_extract.maybe_enqueue_notes_job(
                    _conn, session_id, user_id,
                    provider_url=provider_url, provider_key=provider_key,
                    provider_type=provider_type, model_name=model)
            except Exception:
                _LOG.exception("notes-extract enqueue failed")
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
        # No valid model name: fail loudly instead of silently hitting the backend
        # with some default name (which would produce a confusing 404).
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
    action = None
    content = None
    body = await request.body()
    if body:
        try:
            import json as _json
            data = _json.loads(body)
            confirmed = bool(data.get("confirmed", True))
            remember = bool(data.get("remember", False))
            confirm_id = str(data.get("confirm_id") or "")
            # MCP elicitation extension. Absent for every pre-existing card type,
            # which keeps taking the two-state path untouched.
            raw_action = data.get("action")
            if raw_action is not None:
                action = str(raw_action)
            raw_content = data.get("content")
            if isinstance(raw_content, dict):
                content = raw_content
        except Exception:
            pass
    if not confirm_id:
        raise HTTPException(status_code=400, detail="confirm_id_required")
    if action is not None and action not in _confirm_mod.ELICIT_ACTIONS:
        raise HTTPException(status_code=400, detail="bad_action")
    try:
        _confirm_mgr.resolve(confirm_id, confirmed, remember=remember,
                             expected_session_id=session_id,
                             action=action, content=content)
    except KeyError as e:
        # confirm_expired (id unknown / already resolved / agent restarted) or
        # confirm_session_mismatch (id belongs to another session). Both are 409.
        raise HTTPException(status_code=409, detail=str(e.args[0]) if e.args else "confirm_expired")
    return {"ok": True}


@app.get("/agent/shell-allowlist")
async def shell_allowlist_list(x_user_id: str = Header(..., alias="X-User-Id")):
    from shell_guard import allowlist as _al
    import db as _db
    return {"entries": _al.list_entries(_db.get_connection())}


@app.post("/agent/shell-allowlist")
async def shell_allowlist_add(
    request: Request,
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    from shell_guard import allowlist as _al
    import db as _db
    import json as _json
    try:
        data = _json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="invalid_json")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="invalid_json")
    match_type = data.get("match_type")
    value = data.get("value")
    note = data.get("note") or ""
    if match_type not in ("prefix", "regex", "path_scope"):
        raise HTTPException(status_code=400, detail="bad_match_type")
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail="empty_value")
    if not isinstance(note, str):
        note = ""
    entry_id = _al.add(_db.get_connection(), match_type, value, x_user_id, note)
    return {"id": entry_id}


@app.delete("/agent/shell-allowlist/{entry_id}")
async def shell_allowlist_delete(
    entry_id: str,
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    from shell_guard import allowlist as _al
    import db as _db
    return {"ok": _al.delete(_db.get_connection(), entry_id)}


_TOOLBOX_JOBS: dict[str, "asyncio.Task"] = {}


@app.get("/agent/toolbox")
async def toolbox_list(x_user_id: str = Header(..., alias="X-User-Id")):
    from toolbox import installer
    import db as _db
    return {"components": installer.list_components(_db.get_connection())}


@app.post("/agent/toolbox/install")
async def toolbox_install(request: Request, x_user_id: str = Header(..., alias="X-User-Id")):
    from toolbox import installer
    import db as _db
    try:
        body = await request.json()
        cid = str(body["id"])
    except Exception:
        raise HTTPException(400, "invalid_json")
    try:
        installer._catalog_by_id(cid)
    except installer.InstallError:
        raise HTTPException(404, "unknown_component")
    job = _TOOLBOX_JOBS.get(cid)
    if job and not job.done():
        raise HTTPException(409, "already_installing")

    async def _job():
        try:
            await installer.install(_db.get_connection(), cid)
        except Exception:
            _LOG.exception("toolbox install failed: %s", cid)

    _TOOLBOX_JOBS[cid] = asyncio.create_task(_job())
    return JSONResponse({"status": "installing"}, status_code=202)


@app.post("/agent/toolbox/uninstall")
async def toolbox_uninstall(request: Request, x_user_id: str = Header(..., alias="X-User-Id")):
    from toolbox import installer
    import db as _db
    try:
        body = await request.json()
        cid = str(body["id"])
    except Exception:
        raise HTTPException(400, "invalid_json")
    try:
        installer.uninstall(_db.get_connection(), cid)
    except installer.InstallError:
        raise HTTPException(404, "unknown_component")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Scheduled tasks (M2)
#
# Everything below is scoped by X-User-Id. A task that belongs to somebody
# else must look *absent*, not forbidden: `store.get_task` filters on
# (id, user_id) and a miss is always 404, so this API cannot be used to probe
# for other users' task ids. The store's run-level helpers (set_next_run /
# claim_run / finish_run / list_runs) take no user_id at all — every handler
# here therefore establishes ownership through `get_task` FIRST and only then
# touches runs.
# ---------------------------------------------------------------------------

_TASK_TRIGGERS = ("cron", "interval", "webhook_only")
_TASK_ENUMS = {
    "overlap_policy": ("skip", "queue"),
    "catchup_policy": ("skip", "run_once"),
    "notify_policy": ("failure", "always", "never"),
}
# The scheduler ticks every 15s and a run can outlive its period, so anything
# under a minute is a foot-gun rather than a feature. The store does not
# enforce it (it takes whatever it is given); this is the only gate.
_MIN_INTERVAL_SECONDS = 60
_MAX_TURNS_RANGE = (1, 100)
_TIMEOUT_RANGE = (60, 7200)
_RUNS_LIMIT_RANGE = (1, 200)

# `fs_write` is the one preauth field that can authorize everything in a single
# string: `tasks/driver.fs_allowed` realpaths the entry and prefix-matches, so
# "/" pre-approves every write an unattended run could ever request (verified:
# `fs_allowed("/etc/shadow", ["/"])` is True), and `grants.grant_fs` would then
# register that root as a visible resource. A system root is the same problem
# one level down — it hands an unattended run the agent's own code and
# database, /etc, and the service units that start them. Both are refused at
# the edge with `bad_fs_write` rather than trimmed silently, so the author
# finds out their document was not accepted.
#
# The deny list itself lives in `tasks/driver.py` (single source of truth) and
# is re-applied there at run time, because this check can only judge the string
# as it is today — see `driver.fs_root_denied`.
def _check_fs_write(paths) -> None:
    """Reject `/`, system roots and non-absolute entries in `fs_write`."""
    from tasks.driver import fs_root_denied
    for raw in paths or []:
        path = (raw or "").strip()
        # Relative paths would be resolved against whatever CWD the run
        # happens to have — never what the author meant.
        if not path.startswith("/"):
            raise HTTPException(400, "bad_fs_write")
        # Judge the same string the gate will: it realpaths before matching,
        # so a symlink under an innocuous name must not launder a denied root.
        try:
            real = os.path.realpath(path)
        except (OSError, ValueError):
            # An embedded NUL raises ValueError here (and OSError is possible
            # on some platforms). A path that cannot be resolved cannot be
            # judged, and an unjudgeable rule must not be stored — without
            # this the exception escaped as a 500.
            raise HTTPException(400, "bad_fs_write")
        if fs_root_denied(real):
            raise HTTPException(400, "bad_fs_write")


def _empty_preauth_report() -> dict:
    """A fresh copy every time — the report goes into a response body and a
    shared module-level dict would be one mutation away from leaking between
    requests."""
    return {"truncated": {}, "rejected_rules": []}


def _task_int(value, detail: str) -> int:
    # bool is an int subclass; `True` as a timeout is a client bug, not 1.
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise HTTPException(400, detail)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise HTTPException(400, detail)


def _task_payload(body: dict, existing=None) -> tuple[dict, dict]:
    """Validate a create/update body; return (store fields, preauth report).

    `existing` is None for a create (required fields enforced) and the current
    row for an update. Schedule validity is checked against the MERGED state:
    a PUT that only flips `trigger_type` to `interval` must be rejected when
    the stored `interval_seconds` is still 0, or the scheduler would disable
    the task on its next tick instead.
    """
    if not isinstance(body, dict):
        raise HTTPException(400, "invalid_json")
    creating = existing is None
    out: dict = {}

    for field in ("name", "prompt"):
        if field in body:
            value = body[field]
            if not isinstance(value, str) or not value.strip():
                raise HTTPException(400, f"{field}_required")
            out[field] = value.strip() if field == "name" else value
        elif creating:
            raise HTTPException(400, f"{field}_required")

    if "trigger_type" in body:
        if body["trigger_type"] not in _TASK_TRIGGERS:
            raise HTTPException(400, "bad_trigger_type")
        out["trigger_type"] = body["trigger_type"]
    elif creating:
        raise HTTPException(400, "bad_trigger_type")
    trigger = out.get("trigger_type") or (existing["trigger_type"] if existing else "")

    if "cron_expr" in body:
        if not isinstance(body["cron_expr"], str):
            raise HTTPException(400, "bad_cron")
        out["cron_expr"] = body["cron_expr"].strip()
    cron_expr = out.get("cron_expr", existing["cron_expr"] if existing else "")

    if "interval_seconds" in body:
        out["interval_seconds"] = _task_int(body["interval_seconds"], "bad_interval")
        if out["interval_seconds"] < 0:
            raise HTTPException(400, "bad_interval")
    interval = out.get("interval_seconds",
                       existing["interval_seconds"] if existing else 0)

    if trigger == "cron":
        from tasks import cron as _cron
        try:
            _cron.validate(cron_expr)
            # `validate` only parses the fields; an expression like
            # `0 0 30 2 *` parses fine and never fires, and `store.create_task`
            # would raise CronError computing next_run_at -> a 500. Resolve it
            # here so it comes back as the same 400 the user can act on.
            _cron.next_after(cron_expr, int(time.time()))
        except (_cron.CronError, ValueError, TypeError):
            raise HTTPException(400, "bad_cron")
    elif trigger == "interval" and interval < _MIN_INTERVAL_SECONDS:
        raise HTTPException(400, "bad_interval")

    if "max_turns" in body:
        value = _task_int(body["max_turns"], "bad_max_turns")
        if not (_MAX_TURNS_RANGE[0] <= value <= _MAX_TURNS_RANGE[1]):
            raise HTTPException(400, "bad_max_turns")
        out["max_turns"] = value

    if "timeout_seconds" in body:
        value = _task_int(body["timeout_seconds"], "bad_timeout")
        if not (_TIMEOUT_RANGE[0] <= value <= _TIMEOUT_RANGE[1]):
            raise HTTPException(400, "bad_timeout")
        out["timeout_seconds"] = value

    for field, allowed in _TASK_ENUMS.items():
        if field in body:
            if body[field] not in allowed:
                raise HTTPException(400, f"bad_{field}")
            out[field] = body[field]

    for field in ("agent_type", "model", "notify_channel"):
        if field in body:
            if not isinstance(body[field], str):
                raise HTTPException(400, f"bad_{field}")
            out[field] = body[field].strip()

    if "enabled" in body and not creating:
        out["enabled"] = 1 if body["enabled"] else 0

    if "notify_on_start" in body:
        out["notify_on_start"] = 1 if body["notify_on_start"] else 0

    report = _empty_preauth_report()
    if "preauth" in body:
        from tasks import preauth as _preauth
        # `parse` treats a non-dict as an empty document, so a client that
        # sends a string or a list would get a 201 and an empty report and
        # believe its rules are live. Reject the shape instead.
        if not isinstance(body["preauth"], dict):
            raise HTTPException(400, "bad_preauth")
        # parse_with_report, never parse: a rule this normalizer drops (a
        # leftover regex shell rule) would otherwise be accepted silently and
        # the author would believe their unattended run is pre-authorized.
        doc, report = _preauth.parse_with_report(body["preauth"])
        _check_fs_write(doc["fs_write"])
        out["preauth"] = doc
    return out, report


def _task_out(row) -> dict:
    from tasks import preauth as _preauth
    out = dict(row)
    out["preauth"] = _preauth.parse(out.pop("preauth_json", "{}"))
    out["enabled"] = bool(out.get("enabled"))
    out["notify_on_start"] = bool(out.get("notify_on_start"))
    return out


def _run_out(row) -> dict:
    out = dict(row)
    raw = out.get("denied_actions")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = []
    out["denied_actions"] = raw if isinstance(raw, list) else []
    return out


async def _task_body(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        raise HTTPException(400, "invalid_json")


def _owned_task(task_id: str, user_id: str):
    from tasks import store as _store
    row = _store.get_task(_db(), task_id, user_id)
    if row is None:
        raise HTTPException(404, "not_found")
    return row


@app.get("/agent/tasks")
async def tasks_list(x_user_id: str = Header(..., alias="X-User-Id")):
    from tasks import store as _store
    return {"tasks": [_task_out(r) for r in _store.list_tasks(_db(), x_user_id)]}


# Registered BEFORE /agent/tasks/{task_id}: FastAPI matches routes in
# declaration order, so a literal path added after the parameterized one would
# be swallowed by it and answered with 404 not_found.
@app.get("/agent/tasks/notify-targets")
async def tasks_notify_targets(x_user_id: str = Header(..., alias="X-User-Id")):
    """Chats this user can point `notify_channel` at.

    Sourced from `channel_chats`, which is written the first time a paired
    account actually messages the bot — a just-paired channel legitimately
    does not show up here yet.
    """
    from channels import store as channel_store
    return {"targets": channel_store.list_chats_for_user(_db(), x_user_id)}


# Registered BEFORE /agent/tasks/{task_id} for the same reason
# notify-targets is: FastAPI matches in definition order, so a later
# static path would be swallowed by the {task_id} parameter route.
@app.post("/agent/tasks/draft-from-session")
async def tasks_draft_from_session(
    request: Request,
    x_user_id: str = Header(..., alias="X-User-Id"),
    x_agent_provider_key: str = Header("", alias="X-Agent-Provider-Key"),
    x_agent_provider_url: str = Header("", alias="X-Agent-Provider-Url"),
    x_agent_mcp_ticket: str = Header("", alias="X-Agent-MCP-Ticket"),
):
    """Turn one chat session into a scheduled-task draft.

    READ-ONLY on purpose: it produces a *suggestion*, and the authorization
    it suggests only becomes real when the user saves it through the normal
    POST /agent/tasks path, where preauth.parse and _check_fs_write apply.
    That is why the M5 red line (agent-created tasks must be disabled with an
    empty preauth) does not apply here — see spec §6 and §13.
    """
    from tasks import draft as _draft

    body = await _task_body(request)
    if not isinstance(body, dict):
        raise HTTPException(400, "bad_body")
    session_id = (body.get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(400, "bad_session_id")

    conn = _db()
    row = conn.execute(
        "SELECT id FROM sessions WHERE id=? AND user_id=?",
        (session_id, x_user_id),
    ).fetchone()
    # Absent, not forbidden — same rule as the rest of this section: the API
    # must not confirm that somebody else's session id exists.
    if not row:
        raise HTTPException(404, "session not found")

    last = conn.execute(
        "SELECT content FROM messages WHERE session_id=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    history = []
    if last:
        try:
            history = json.loads(last["content"]) or []
        except (json.JSONDecodeError, TypeError):
            history = []
    if not isinstance(history, list):
        history = []

    # slug → server id. Without a ticket (or with an unusable config) the map
    # is simply empty and every MCP call lands in evidence["dropped"]: a
    # missing suggestion, never a wrong one.
    mcp_id_by_slug = {}
    if x_agent_mcp_ticket:
        try:
            from mcp_client.runtime import fetch_mcp_servers
            from mcp_client.client import _slug
            servers = await fetch_mcp_servers(x_agent_mcp_ticket)
            if isinstance(servers, list):
                for s in servers:
                    if not isinstance(s, dict) or not s.get("id") or not s.get("name"):
                        continue
                    try:
                        mcp_id_by_slug[_slug(s["name"])] = str(s["id"])
                    except Exception:
                        # 一条畸形条目不该让其余服务器的建议一起消失
                        continue
        except Exception:
            _LOG.exception("draft-from-session: MCP server list unavailable; "
                           "MCP tools will not be suggested")

    scanned = _draft.scan_history(history, mcp_id_by_slug=mcp_id_by_slug)

    name = _draft.fallback_name(history)
    prompt = _draft.fallback_prompt(history)
    fallback = True

    model = (body.get("model") or "").strip()
    if model and history:
        try:
            # `history` is untrusted JSON out of the DB and
            # extract_history_excerpt assumes every item is a dict, so it
            # belongs INSIDE the guard: a malformed history must degrade to
            # the fallback draft, never to a 500.
            excerpt = title_gen.extract_history_excerpt(history)
            if excerpt:
                client = AsyncOpenAI(base_url=x_agent_provider_url,
                                     api_key=x_agent_provider_key)
                resp = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": _draft.DRAFT_SYSTEM_PROMPT},
                            {"role": "user", "content": excerpt},
                        ],
                        temperature=0.3,
                        max_tokens=1024,
                    ),
                    timeout=30.0,
                )
                raw = ""
                if resp.choices:
                    msg = resp.choices[0].message
                    raw = (getattr(msg, "content", None)
                           or getattr(msg, "reasoning_content", None) or "")
                parsed = _draft.parse_llm_draft(raw)
                if parsed:
                    name, prompt = parsed
                    fallback = False
        except (asyncio.TimeoutError, Exception):
            # Never fail the request over the model: a fallback draft the
            # user edits by hand still beats an error dialog.
            _LOG.exception("draft-from-session: model unavailable; "
                           "falling back to raw user messages")

    return {
        "name": name,
        "prompt": prompt,
        "preauth": scanned["preauth"],
        "suggested_egress": scanned["suggested_egress"],
        "evidence": scanned["evidence"],
        "prompt_fallback": fallback,
    }


@app.post("/agent/tasks", status_code=201)
async def tasks_create(request: Request,
                       x_user_id: str = Header(..., alias="X-User-Id")):
    from tasks import store as _store
    fields, report = _task_payload(await _task_body(request))
    task_id = _store.create_task(_db(), x_user_id, **fields)
    return {"id": task_id, "preauth_report": report}


@app.get("/agent/tasks/{task_id}")
async def tasks_get(task_id: str, x_user_id: str = Header(..., alias="X-User-Id")):
    return _task_out(_owned_task(task_id, x_user_id))


@app.put("/agent/tasks/{task_id}")
async def tasks_update(task_id: str, request: Request,
                       x_user_id: str = Header(..., alias="X-User-Id")):
    from tasks import store as _store
    existing = _owned_task(task_id, x_user_id)
    fields, report = _task_payload(await _task_body(request), existing)
    _store.update_task(_db(), task_id, x_user_id, **fields)
    return {"status": "ok", "preauth_report": report}


@app.delete("/agent/tasks/{task_id}", status_code=204)
async def tasks_delete(task_id: str,
                       x_user_id: str = Header(..., alias="X-User-Id")):
    from tasks import store as _store
    # Awaited: delete_task also reclaims the task's runs and the sessions they
    # own (see its docstring — without that, deleting a task orphans every row
    # it ever produced, with nothing left pointing at them).
    if not await _store.delete_task(_db(), task_id, x_user_id):
        raise HTTPException(404, "not_found")
    return Response(status_code=204)


@app.post("/agent/tasks/{task_id}/run", status_code=202)
async def tasks_run_now(task_id: str,
                        x_user_id: str = Header(..., alias="X-User-Id")):
    """Queue one run immediately. The runner worker picks it up on its own.

    Allowed on a disabled task on purpose: "run it now" is how a user tests a
    task before switching the schedule on. overlap_policy is not consulted
    either — an explicit human request outranks it.
    """
    from tasks import store as _store
    _owned_task(task_id, x_user_id)
    run_id = _store.create_run(_db(), task_id, x_user_id, "manual")
    return JSONResponse({"run_id": run_id}, status_code=202)


@app.post("/agent/tasks/{task_id}/webhook-token/reset")
async def tasks_reset_webhook_token(task_id: str,
                                    x_user_id: str = Header(..., alias="X-User-Id")):
    """Issue a fresh webhook token, invalidating the old one immediately."""
    from tasks import store as _store
    _owned_task(task_id, x_user_id)
    token = _store.reset_webhook_token(_db(), task_id, x_user_id)
    if not token:
        raise HTTPException(404, "not_found")
    return {"webhook_token": token}


# The webhook trigger. This is the ONE agent endpoint with no JWT: the Go layer
# skips authentication for its route and strips every identity header, so the
# task's own token is the entire credential and the owner comes from the task
# row — never from the request.
#
# Deliberately NOT under /agent/tasks/: that whole subtree is admin-scoped
# (route/v2/admin_guard.go), which would demand an admin JWT and defeat the
# point. A sibling path needs no exception carved into the admin gate.
#
# The request body is not read at all. Phase one accepts no parameters (spec
# §9): anything a caller could inject would reach the model as instructions.
@app.post("/agent/task-webhook/{token}")
async def task_webhook_trigger(token: str):
    from tasks import store as _store
    from tasks.webhook import RATE_LIMITER

    task = _store.get_task_by_webhook_token(_db(), token)
    # Unknown token is absent, not forbidden — the same rule the rest of this
    # API follows, and here it also avoids confirming that a token was close.
    if task is None:
        raise HTTPException(404, "not_found")
    # `run now` works on a disabled task on purpose (that is how a human tests
    # one). A webhook fires unattended, so disabled has to mean disabled.
    if not task["enabled"]:
        raise HTTPException(409, "task_disabled")
    if not RATE_LIMITER.allow(task["id"]):
        raise HTTPException(429, "rate_limited")

    run_id = _store.create_run(_db(), task["id"], task["user_id"], "webhook")
    return JSONResponse({"run_id": run_id}, status_code=202)


@app.get("/agent/tasks/{task_id}/runs")
async def tasks_runs(task_id: str, limit: int = 50,
                     x_user_id: str = Header(..., alias="X-User-Id")):
    from tasks import store as _store
    _owned_task(task_id, x_user_id)
    limit = max(_RUNS_LIMIT_RANGE[0], min(_RUNS_LIMIT_RANGE[1], limit))
    return {"runs": [_run_out(r)
                     for r in _store.list_runs(_db(), task_id, limit)]}


def _preauth_from_denied(doc: dict, action: dict) -> tuple[dict, str, object]:
    """Fold one denied action into a preauth document.

    Returns `(new document, bucket, adopted entry)` — a copy, never a mutation
    of `doc`. The vocabulary is `tasks/driver.py`'s normalized kinds; `detail`
    is what that driver recorded — for `fs` the FIRST path that was not
    covered, not the card's first path, so the rule generated here actually
    changes the outcome next time.
    """
    kind = str(action.get("kind") or "")
    raw_detail = str(action.get("detail") or "")
    detail = raw_detail.strip()
    if not detail:
        raise HTTPException(400, "empty_detail")

    out = {k: list(v) for k, v in doc.items()}
    if kind == "egress":
        from tasks.driver import _strip_port
        # Bare host, no port: that is what the egress gate matches on.
        entry, bucket = _strip_port(detail), "egress_domains"
    elif kind == "fs":
        # A denied file grants its directory — `fs_write` entries are roots
        # and a bare file path would authorize nothing else in that folder.
        entry = os.path.dirname(detail) if os.path.isfile(detail) else detail
        bucket = "fs_write"
        # Same gate as create/update: adopting a denial must not become the
        # back door that puts "/" (or /etc) into a preauth document.
        _check_fs_write([entry])
    elif kind == "mcp_tool":
        entry, bucket = detail, "mcp_tools"          # already "server::tool"
    elif kind == "shell":
        parts = detail.split()
        # A denied `<interpreter> <absolute script>` becomes a `scripts` entry,
        # not a prefix rule. Without this branch the button is a dead end for
        # the whole "run my collector every morning" case: the prefix generated
        # below would be `python3 `, which the run gate refuses outright
        # (interpreter), so `run_allowlist_would_cover` correctly rejects it and
        # the user sees `shell_rule_would_not_apply` with nothing to do about it
        # — the feature would exist but be unreachable from the one place a user
        # actually meets it.
        # `script_run_target`, NOT `run_scripts_would_cover`: the latter answers
        # True for every `safe` command whatever the rules, so using it as a
        # detector read `lark-cli mail list --limit 5` as a script run and
        # adopted `5` as the script path.
        from skills import shell as _shell
        _script = _shell.script_run_target(raw_detail)
        if _script:
            bucket, entry = "scripts", _script
            if entry not in out[bucket]:
                out[bucket].append(entry)
            return out, bucket, entry
        # `preauth.shell_match` is `command.startswith(value)` on the RAW
        # command — deliberately not stripped there, since leading whitespace
        # is part of what the author would have had to authorize. So the rule
        # has to carry the same leading whitespace the denied command had, or
        # adopting `"  rm -rf x"` would generate `"rm "`, which can never
        # match it and leaves the button a silent no-op.
        lead = raw_detail[:len(raw_detail) - len(raw_detail.lstrip())]
        # Head + a space, so `git ` can never also authorize `github-cli`.
        # A command that WAS just its head ("date") is the exception: `"date "`
        # could never prefix-match it either, so the bare token is stored.
        entry = {"kind": "prefix",
                 "value": lead + parts[0] + ("" if len(parts) == 1 else " ")}
        bucket = "shell"
        # A head-derived prefix cannot authorize every command it came from.
        # The run gate refuses chaining, redirection, interpreters and
        # `protected` outright — whatever the rules say — so for those the rule
        # written here would be inert, and the user would walk away believing
        # the next run is authorized. Ask the gate itself rather than
        # re-deriving its conditions, and refuse instead of writing a no-op.
        # (`_shell` is already imported by the scripts branch above.)
        if not _shell.run_allowlist_would_cover(raw_detail, [entry]):
            raise HTTPException(400, "shell_rule_would_not_apply")
    else:
        raise HTTPException(400, "unsupported_kind")

    if not entry:
        raise HTTPException(400, "empty_detail")
    if entry not in out[bucket]:
        out[bucket].append(entry)
    return out, bucket, entry


@app.post("/agent/tasks/{task_id}/preauth/from-denied")
async def tasks_preauth_from_denied(
        task_id: str, request: Request,
        x_user_id: str = Header(..., alias="X-User-Id")):
    """Adopt one denied action from a past run into the task's preauth."""
    from tasks import preauth as _preauth
    from tasks import store as _store
    task = _owned_task(task_id, x_user_id)
    body = await _task_body(request)
    if not isinstance(body, dict):
        raise HTTPException(400, "invalid_json")
    run_id = str(body.get("run_id") or "")
    index = _task_int(body.get("index", 0), "bad_index")

    # Scoped on task_id AND user_id: `store.list_runs`/`finish_run` take no
    # user_id, so ownership is established here or not at all.
    run = _db().execute(
        "SELECT * FROM task_runs WHERE id=? AND task_id=? AND user_id=?",
        (run_id, task_id, x_user_id)).fetchone()
    if run is None:
        raise HTTPException(404, "not_found")
    denied = _run_out(run)["denied_actions"]
    if index < 0 or index >= len(denied) or not isinstance(denied[index], dict):
        raise HTTPException(404, "denied_action_not_found")

    doc = _preauth.parse(task["preauth_json"])
    doc, bucket, entry = _preauth_from_denied(doc, denied[index])
    # Re-normalize: MAX_RULES truncation applies to a grown document too.
    doc, report = _preauth.parse_with_report(doc)
    if entry not in doc[bucket]:
        # The bucket was already at MAX_RULES, so normalization dropped the
        # very rule this call was supposed to add. Writing the document back
        # and answering 200 would tell the user their action was adopted when
        # nothing changed; refuse instead so they know to prune first.
        raise HTTPException(400, "preauth_full")
    _store.update_task(_db(), task_id, x_user_id, preauth=doc)
    return {"preauth": doc, "preauth_report": report,
            "adopted": {"field": bucket, "value": entry}}


def _lark_uid(x_user_id: str) -> str:
    """Validate the user id before it is used to build a filesystem path.

    `binding.user_home()` joins this onto HOMES_ROOT, so a `../` would escape
    the per-user home (and DELETE would rmtree outside it). Rejected at the
    edge, so nothing downstream has to trust it.
    """
    from lark import binding as _lark
    if not _lark.valid_uid(x_user_id):
        raise HTTPException(400, "invalid_user_id")
    return x_user_id


@app.post("/agent/lark/binding")
async def lark_binding_start(x_user_id: str = Header(..., alias="X-User-Id")):
    from lark import binding as _lark
    return JSONResponse(await _lark.start(_lark_uid(x_user_id)), status_code=202)


@app.get("/agent/lark/binding")
async def lark_binding_status(x_user_id: str = Header(..., alias="X-User-Id")):
    # Never 404s: a user who has never bound simply reports phase=unbound.
    from lark import binding as _lark
    return await _lark.status(_lark_uid(x_user_id))


@app.delete("/agent/lark/binding", status_code=204)
async def lark_binding_delete(x_user_id: str = Header(..., alias="X-User-Id")):
    from lark import binding as _lark
    await _lark.unbind(_lark_uid(x_user_id))
    return Response(status_code=204)


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


@app.get("/agent/observability/compose")
async def get_observability_compose():
    import os
    from fastapi.responses import PlainTextResponse
    path = os.path.join(os.path.dirname(__file__), "observability", "phoenix_compose.yaml")
    with open(path, "r", encoding="utf-8") as f:
        return PlainTextResponse(f.read(), media_type="application/yaml")


# ---------------------------------------------------------------------------
# Knowledge notes API (M2). Files are the content authority; these endpoints
# are the UI's metadata/CRUD surface. Identity via X-User-Id (Go proxy strips
# JWT and injects it). Settings is admin-gated at the Go layer (route/v2.go).
# ---------------------------------------------------------------------------
from notes import reserved as notes_reserved
from notes import store as notes_store
from notes.indexer import index_note as notes_index_note
from notes.indexer import deindex_note as notes_deindex_note


class NoteCreatePayload(BaseModel):
    title: str
    content: str
    note_type: str = "note"
    tags: list[str] = []
    source_refs: list[dict] = []
    description: str = ""


class NoteUpdatePayload(BaseModel):
    expected_revision: int
    content: str | None = None
    title: str | None = None
    status: str | None = None
    tags: list[str] | None = None
    description: str | None = None


class NotesSettingsPayload(BaseModel):
    notes_root: str | None = None
    mode: str = "adopt"          # adopt | migrate
    auto_extract: bool | None = None
    distill_roots: list[str] | None = None
    distill_daily_cap: int | None = None
    background_model: str | None = None


def _notes_uid(request: Request) -> str:
    uid = request.headers.get("X-User-Id", "")
    if not uid:
        raise HTTPException(status_code=401, detail="X-User-Id required")
    return uid


async def _notes_post_write(conn, uid: str, note: dict, body: str) -> None:
    ok = await notes_index_note(note, body)
    if not ok:
        conn.execute("UPDATE notes SET content_hash='' WHERE id=? AND user_id=?",
                     (note["id"], uid))
        conn.commit()
    try:
        notes_reserved.render_for_user(conn, uid)
    except Exception:
        logging.getLogger("nimoos-agent").exception("reserved render failed")


def _notes_settings_body(conn, uid: str) -> dict:
    return {"notes_root": notes_store.get_notes_root(conn),
            "auto_extract": notes_store.is_auto_extract_enabled(conn, uid),
            "distill_roots": notes_store.get_distill_roots(conn, uid),
            "distill_daily_cap": notes_store.get_daily_cap(conn, uid),
            "background_model": notes_store.get_background_model(conn, uid)}


@app.get("/agent/notes/settings")
async def get_notes_settings(request: Request):
    uid = _notes_uid(request)
    conn = _db()
    return _notes_settings_body(conn, uid)


@app.put("/agent/notes/settings")
async def put_notes_settings(request: Request, body: NotesSettingsPayload):
    uid = _notes_uid(request)
    conn = _db()
    if body.mode not in ("adopt", "migrate"):
        raise HTTPException(status_code=400, detail="mode must be adopt|migrate")
    if body.auto_extract is not None:
        notes_store.set_auto_extract(conn, uid, body.auto_extract)
    if body.notes_root:
        old = notes_store.get_notes_root(conn)
        new = os.path.abspath(body.notes_root)
        if new != old:
            if body.mode == "migrate":
                if os.path.isdir(new) and os.listdir(new):
                    raise HTTPException(status_code=400,
                                        detail="migrate target is not empty — choose an "
                                               "empty directory or use mode=adopt")
                os.makedirs(new, exist_ok=True)
                for entry in sorted(os.listdir(old)) if os.path.isdir(old) else []:
                    shutil.move(os.path.join(old, entry), os.path.join(new, entry))
            notes_store.set_notes_root(conn, new)   # rel path unchanged, identity tracked via frontmatter id
    if body.distill_roots is not None:
        notes_store.set_distill_roots(conn, uid, body.distill_roots)
    if body.distill_daily_cap is not None:
        if body.distill_daily_cap < 0:
            raise HTTPException(status_code=400,
                                detail="distill_daily_cap must be >= 0")
        notes_store.set_daily_cap(conn, uid, body.distill_daily_cap)
    if body.background_model is not None:
        notes_store.set_background_model(conn, uid, body.background_model)
    return _notes_settings_body(conn, uid)


# Probes are confined to the user-visible data root (tests monkeypatch this).
_NOTES_PROBE_ROOT = "/DATA"


# Registered before /agent/notes/{note_id} so "dir-info" is not captured as an id.
@app.get("/agent/notes/dir-info")
async def notes_dir_info(request: Request, path: str = ""):
    """Probe a candidate notes folder for the settings UI: same emptiness
    semantics as the migrate guard in put_notes_settings (any entry counts,
    dotfiles included). A missing directory is migratable (migrate mkdirs it)."""
    _notes_uid(request)
    p = os.path.abspath(path or "")
    if p != _NOTES_PROBE_ROOT and not p.startswith(_NOTES_PROBE_ROOT + "/"):
        raise HTTPException(status_code=400,
                            detail=f"path must be under {_NOTES_PROBE_ROOT}")
    exists = os.path.isdir(p)
    if not exists:
        return {"exists": False, "empty": True}
    try:
        empty = not os.listdir(p)
    except OSError:
        # Unreadable: report non-empty so the UI doesn't promise a migrate
        # the backend guard would then reject.
        empty = False
    return {"exists": True, "empty": empty}


class DistillRequestPayload(BaseModel):
    path: str


# The three roots Parser's extract accepts (its EXTRACT_ROOTS); the same
# gate logic (realpath containment, .system_data carve) applies per root.
# fs_gate itself stays /DATA-only for the external MCP surface.
_DISTILL_GATE_ROOTS = ("/DATA", "/media", "/mnt")


def _distill_gate_ok(user_id: str, path: str) -> bool:
    """Headless deny-only gate — same fs_gate logic MCP path reads use,
    widened to the extract roots. Never a second file-access route
    (DEVELOPMENT_PLAN ban #5)."""
    from mcp_server import fs_gate
    for root in _DISTILL_GATE_ROOTS:
        try:
            fs_gate.mcp_resolve_read_path(path, root=root)
            return True
        except fs_gate.McpPathDenied:
            continue
    return False


@app.post("/agent/notes/distill")
async def notes_distill_manual(request: Request, body: DistillRequestPayload):
    import notes_distill
    uid = _notes_uid(request)
    path = os.path.abspath(body.path)
    if not _distill_gate_ok(uid, path):
        raise HTTPException(status_code=403, detail="path not allowed")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="file not found")
    if not notes_distill.is_distillable(path):
        raise HTTPException(status_code=400, detail="unsupported document type")
    conn = _db()
    notes_distill.enqueue(conn, file_path=path, user_id=uid, root_id="",
                          file_mtime=int(os.stat(path).st_mtime),
                          origin="manual")
    return {"queued": True}


@app.get("/agent/notes/distill/status")
async def notes_distill_status(request: Request):
    uid = _notes_uid(request)
    conn = _db()
    pending = conn.execute(
        "SELECT COUNT(*) c FROM notes_distill_jobs WHERE user_id=? "
        "AND status IN ('pending','running')",
        (uid,)).fetchone()["c"]
    distilled = conn.execute(
        "SELECT COUNT(*) c FROM notes WHERE user_id=? AND type='summary' "
        "AND created_by='pipeline' AND deleted_at IS NULL",
        (uid,)).fetchone()["c"]
    day = time.strftime("%Y%m%d")
    return {
        "pending": pending,
        "distilled": distilled,
        "quota_remaining": notes_store.quota_remaining(conn, uid, day=day),
        "background_model": notes_store.get_background_model(conn, uid),
    }


@app.get("/agent/notes/distill/jobs")
async def notes_distill_jobs(request: Request, status: str = "",
                             limit: int = 200):
    uid = _notes_uid(request)
    conn = _db()
    limit = max(1, min(int(limit), 500))
    where = "user_id=?"
    args: list = [uid]
    if status:
        if status not in ("pending", "running", "failed"):
            raise HTTPException(400, "status must be pending|running|failed")
        if status == "failed":
            # Tombstones: 'failed' (retries exhausted) and 'skipped'
            # (terminal drop) are one bucket to the user; rows keep the
            # raw status so the UI can badge them apart.
            where += " AND status IN ('failed','skipped')"
        else:
            where += " AND status=?"
            args.append(status)
    rows = conn.execute(
        f"SELECT file_path, status, origin, attempts, last_error, "
        f"enqueued_at, updated_at FROM notes_distill_jobs WHERE {where} "
        f"ORDER BY updated_at DESC LIMIT ?", (*args, limit)).fetchall()
    counts = {"pending": 0, "running": 0, "failed": 0}
    for r in conn.execute(
            "SELECT status, COUNT(*) c FROM notes_distill_jobs "
            "WHERE user_id=? GROUP BY status", (uid,)):
        key = "failed" if r["status"] in ("failed", "skipped") else r["status"]
        if key in counts:
            counts[key] += r["c"]
    return {"jobs": [dict(r) for r in rows], "counts": counts}


@app.post("/agent/notes/distill/jobs/cancel")
async def notes_distill_cancel(body: DistillRequestPayload, request: Request):
    import notes_distill
    uid = _notes_uid(request)
    conn = _db()
    path = os.path.abspath(body.path)
    cur = conn.execute(
        "UPDATE notes_distill_jobs SET status='skipped', "
        "last_error=?, updated_at=? "
        "WHERE file_path=? AND user_id=? AND status='pending'",
        (notes_distill.CANCELLED_BY_USER, int(time.time()), path, uid))
    conn.commit()
    if cur.rowcount == 0:
        # Not found, not yours, or already claimed/terminal — one answer:
        # nothing cancellable at this path right now.
        raise HTTPException(409, "no pending job for this path")
    return {"cancelled": True}


@app.get("/agent/notes")
async def list_notes_api(request: Request, type: str = "", status: str = "",
                         limit: int = 50):
    uid = _notes_uid(request)
    rows = notes_store.list_notes(_db(), uid, note_type=type or None,
                                  status=status or None,
                                  limit=max(1, min(limit, 200)))
    return {"notes": rows}


@app.post("/agent/notes", status_code=201)
async def create_note_api(request: Request, body: NoteCreatePayload):
    uid = _notes_uid(request)
    conn = _db()
    try:
        note = notes_store.create_note(
            conn, uid, title=body.title, body=body.content,
            note_type=body.note_type, tags=body.tags,
            source_refs=body.source_refs, created_by="human",
            description=body.description)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _notes_post_write(conn, uid, note, body.content)
    return note


@app.get("/agent/notes/{note_id}")
async def get_note_api(note_id: str, request: Request):
    uid = _notes_uid(request)
    note = notes_store.get_note(_db(), uid, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="not found")
    return note


@app.put("/agent/notes/{note_id}")
async def update_note_api(note_id: str, request: Request,
                          body: NoteUpdatePayload):
    uid = _notes_uid(request)
    conn = _db()
    try:
        note = notes_store.update_note(
            conn, uid, note_id, expected_revision=body.expected_revision,
            title=body.title, body=body.content, status=body.status,
            tags=body.tags, description=body.description)
    except KeyError:
        raise HTTPException(status_code=404, detail="not found")
    except notes_store.RevisionConflict as e:
        return JSONResponse(status_code=409, content={
            "detail": "revision conflict",
            "current_revision": e.current_revision})
    await _notes_post_write(conn, uid, note, note["body"])
    return note


@app.delete("/agent/notes/{note_id}")
async def delete_note_api(note_id: str, request: Request):
    uid = _notes_uid(request)
    conn = _db()
    if not notes_store.soft_delete_note(conn, uid, note_id):
        raise HTTPException(status_code=404, detail="not found")
    await notes_deindex_note(uid, note_id)
    try:
        notes_reserved.render_for_user(conn, uid)
    except Exception:
        pass
    return {"status": "deleted", "id": note_id}


async def _set_status(note_id: str, request: Request, status: str):
    uid = _notes_uid(request)
    conn = _db()
    cur = notes_store.get_note(conn, uid, note_id)
    if cur is None:
        raise HTTPException(status_code=404, detail="not found")
    note = notes_store.update_note(conn, uid, note_id,
                                   expected_revision=cur["revision"],
                                   status=status)
    await _notes_post_write(conn, uid, note, note["body"])
    return note


@app.post("/agent/notes/{note_id}/curate")
async def curate_note_api(note_id: str, request: Request):
    return await _set_status(note_id, request, "curated")


@app.post("/agent/notes/{note_id}/archive")
async def archive_note_api(note_id: str, request: Request):
    return await _set_status(note_id, request, "archived")


@app.get("/agent/notes/{note_id}/backlinks")
async def note_backlinks_api(note_id: str, request: Request):
    uid = _notes_uid(request)
    return {"backlinks": notes_store.get_backlinks(_db(), uid, note_id)}


# ---------------------------------------------------------------------------
# Web tools settings. One global row: the box shares one search backend
# because the owner pays for the key.
#
# Admin gating lives in the Go layer as an explicit per-route pair in
# route/v2.go — `g.Any("/agent/web-settings", agent.Proxy,
# v2.AdminOnly(runtimePath))`, registered ahead of the /agent/* wildcard,
# same shape as /agent/notes/settings. Until that line exists this endpoint
# is reachable by any authenticated user, so it must not ship without it.
# ---------------------------------------------------------------------------
from web import settings as web_settings_mod


class WebSettingsPayload(BaseModel):
    backend: str = ""
    api_key: str | None = None      # None = keep whatever is stored
    base_url: str = ""
    enabled: bool = False


@app.get("/agent/web-settings")
async def get_web_settings():
    return web_settings_mod.public_view(web_settings_mod.load(_db()))


@app.put("/agent/web-settings")
async def put_web_settings(body: WebSettingsPayload):
    if body.backend and body.backend not in web_settings_mod.VALID_BACKENDS:
        raise HTTPException(
            status_code=400,
            detail=f"backend must be one of {web_settings_mod.VALID_BACKENDS}")
    conn = _db()
    current = web_settings_mod.load(conn)
    # The UI never receives the key, so it cannot echo one back: an omitted
    # api_key means "unchanged", not "clear it". Sending "" clears it.
    api_key = current["api_key"] if body.api_key is None else body.api_key
    web_settings_mod.save(conn, backend=body.backend, api_key=api_key,
                          base_url=body.base_url, enabled=body.enabled)
    return web_settings_mod.public_view(web_settings_mod.load(conn))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8282)
