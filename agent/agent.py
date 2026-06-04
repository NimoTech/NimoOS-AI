import asyncio
import base64
import json
import os
import sqlite3
import time
import uuid
from typing import AsyncIterator

from agents import Agent, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.reasoning_content_replay import default_should_replay_reasoning_content
from openai import AsyncOpenAI

import db as db_module
from provider_adapters import (
    ProviderType, ThinkingConfig, build_model_settings,
)
from skills import ALL_TOOLS
from skills.app_management import (
    SESSION_ID_VAR as APP_SESSION_VAR,
    EVENT_QUEUE_VAR as APP_EVENT_VAR,
    CONFIRM_MGR_VAR as APP_CONFIRM_VAR,
)
import skills.message_bus as mb_skills
import skills.filesystem as fs_skills
from fs import access_request as fs_access_request
import skills.shell as shell_skills
import skills.init_doc as init_doc
import skills.wiki as wiki_skills
import skills.skills_registry as skills_registry
import skills.search as search_skills
from fs.snapshots import SnapshotStore
from wiki_client import WikiClient
from wiki_context import WikiContextBuilder

SYSTEM_PROMPT = """You are Nimo, a general-purpose AI assistant that also has the ability to manage the user's NimoOS NAS.

Treat NAS management as one of many capabilities, not your sole purpose. You can:
- Have casual conversations, answer general questions, brainstorm, explain things.
- Help with code: write, review, refactor, debug, explain across any language or stack.
- Help with writing, math, analysis, planning, learning — like any capable assistant.
- Manage the user's NAS using your tools when the user actually asks for that:
  applications (list/search/install/start/stop/restart/uninstall/update),
  storage, services, and MessageBus actions.

Behavior rules:
- Do not refuse or redirect non-NAS requests by claiming you only manage NAS. Help with whatever the user asks, the way any general assistant would.
- Only invoke tools when the user is asking about *their NAS* or about an action that needs them. Don't tool-call to write a poem or answer a coding question.
- For read-only NAS operations, act immediately.
- For write NAS operations (install, start, stop, restart, uninstall, update, trigger), call the tool — the system shows the user a confirmation prompt automatically.
- 文件访问:用户提到的路径即使尚未授权,你也应直接尝试相应文件操作(list/read/write 等)。系统会在需要时自动弹卡片向用户申请该路径的访问授权——不要因为"可能没权限"就预先拒绝或改口。
- 若某次文件操作返回"用户拒绝了对 X 的访问",你必须立即停止当前任务并向用户说明原因;绝对不要改去访问其父目录、兄弟目录或换别的路径来绕过。
- 批量文件结构操作:需要同时执行 2 个或以上的新建文件夹、移动/重命名、删除操作时,必须使用 `batch_fs` 工具一次性完成,而不是多次单独调用。`write_file`/`edit_file` 仅用于修改文件内容。
- Match the user's language. Be concise by default; expand when the task warrants it."""

_SNAPSHOT_STORE = SnapshotStore()

_session_locks: dict[str, asyncio.Lock] = {}


def _get_lock(session_id: str) -> asyncio.Lock:
    if session_id not in _session_locks:
        _session_locks[session_id] = asyncio.Lock()
    return _session_locks[session_id]


def _compose_system_prompt(conn, session_id: str, base: str,
                            *, max_per_file: int = 8 * 1024,
                            max_total: int = 32 * 1024) -> str:
    rows = conn.execute(
        "SELECT path, kind FROM visible_resources WHERE session_id=? "
        "ORDER BY added_at",
        (session_id,),
    ).fetchall()
    if not rows:
        return base + (
            "\n\nNo filesystem resources are currently authorized. "
            "The user can grant access via @-mention or the right panel."
        )
    summary_lines = ["",
                     "You currently have access to the following filesystem "
                     "resources (reads return immediately; writes enter a "
                     "staging area for user review):",
                     ""]
    md_blocks: list[str] = []
    total = 0
    truncated = 0
    for r in rows:
        marker = ""
        if r["kind"] == "folder":
            md_path = os.path.join(r["path"], "agent.md")
            has_md = os.path.isfile(md_path)
            if has_md:
                marker = ", has agent.md"
            summary_lines.append(f"- {r['path']} (folder{marker})")
            if has_md:
                if total >= max_total:
                    truncated += 1
                else:
                    try:
                        with open(md_path, "r", encoding="utf-8",
                                  errors="replace") as f:
                            body = f.read(max_per_file)
                    except OSError:
                        body = ""
                    if body:
                        if total + len(body) > max_total:
                            truncated += 1
                        else:
                            md_blocks.append(
                                f"--- {md_path} ---\n{body}\n"
                            )
                            total += len(body)
        else:
            summary_lines.append(f"- {r['path']} (single file)")
    block = "\n".join(summary_lines)
    if md_blocks:
        block += "\n\nagent.md notes from authorized folders:\n\n"
        block += "\n".join(md_blocks)
    if truncated:
        block += f"\n[...{truncated} more agent.md files truncated]"
    return base + block


def _fetch_attachments(attachment_ids, session_id):
    """Return rows for the given attachment_ids scoped to session_id,
    ordered by created_at. Returns [] when attachment_ids is empty."""
    if not attachment_ids:
        return []
    conn = db_module.get_connection()
    placeholders = ",".join(["?"] * len(attachment_ids))
    rows = conn.execute(
        f"SELECT id, filename, mime, kind, size_bytes, rel_path "
        f"FROM attachments WHERE id IN ({placeholders}) AND session_id = ? "
        f"ORDER BY created_at",
        (*attachment_ids, session_id),
    ).fetchall()
    return list(rows)


def build_user_content(message: str, attachment_ids, *,
                       session_id: str, data_root: str,
                       model_name: str = "", provider_type: str = "other"):
    """Compose the SDK `input` content for the user turn.

    Returns a string when no attachments (backward compat). Otherwise returns
    a list of content blocks. For image kinds: inline base64 image_url block
    when the (provider_type, model_name) supports vision; otherwise a text
    fallback note describing the image.
    """
    if not attachment_ids:
        return message

    from provider_adapters import model_supports_vision
    has_vision = model_supports_vision(provider_type, model_name)

    blocks = [{"type": "input_text", "text": message}]
    degraded_notes = []
    for row in _fetch_attachments(attachment_ids, session_id):
        if row["kind"] != "image":
            continue
        full = os.path.join(data_root, "sessions", session_id, "attachments",
                            row["rel_path"])
        if has_vision:
            try:
                with open(full, "rb") as f:
                    data = f.read()
            except FileNotFoundError:
                continue
            b64 = base64.b64encode(data).decode("ascii")
            blocks.append({
                "type": "input_image",
                "image_url": f"data:{row['mime']};base64,{b64}",
            })
        else:
            kb = max(1, row["size_bytes"] // 1024)
            degraded_notes.append(
                f"[image attachment {row['filename']}, {kb} KB, model does not support vision]"
            )
    if degraded_notes:
        blocks.append({"type": "input_text", "text": "\n".join(degraded_notes)})
    return blocks


def hydrate_image_blocks(history, *, session_id: str, data_root: str):
    """Inverse of `compact_image_blocks`: replace stored compact
    `{type:input_image, attachment_id}` blocks with real
    `{type:input_image, image_url:"data:<mime>;base64,..."}` blocks by
    reading the attachment file from disk.

    History rows are saved in the compact shape to keep SQLite small, but
    the OpenAI Agents SDK's chat-completions adapter only accepts
    image_url-style blocks; feeding back the compact shape on a follow-up
    turn raises "Only image URLs are supported for input_image".

    Blocks whose attachment row or file is missing are dropped silently —
    that's better than re-raising and breaking the whole conversation.
    """
    out = []
    for item in history:
        content = item.get("content")
        if not isinstance(content, list):
            out.append(item)
            continue
        new_content = []
        for blk in content:
            if (isinstance(blk, dict)
                    and blk.get("type") == "input_image"
                    and "attachment_id" in blk
                    and "image_url" not in blk):
                aid = blk["attachment_id"]
                row = db_module.get_connection().execute(
                    "SELECT mime, rel_path FROM attachments "
                    "WHERE id=? AND session_id=?",
                    (aid, session_id),
                ).fetchone()
                if row is None:
                    continue
                full = os.path.join(data_root, "sessions", session_id,
                                    "attachments", row["rel_path"])
                try:
                    with open(full, "rb") as f:
                        data = f.read()
                except FileNotFoundError:
                    continue
                b64 = base64.b64encode(data).decode("ascii")
                new_content.append({
                    "type": "input_image",
                    "image_url": f"data:{row['mime']};base64,{b64}",
                })
            else:
                new_content.append(blk)
        item = {**item, "content": new_content}
        out.append(item)
    return out


def compact_image_blocks(history, *, image_id_resolver):
    """Walk the SDK history; replace any inline image data URL with a compact
    `{type: input_image, attachment_id: <id>}` block.

    `image_id_resolver(url) -> attachment_id | None` is called for each
    image_url found. Return None to leave the block unchanged.
    """
    out = []
    for item in history:
        content = item.get("content")
        if isinstance(content, list):
            new_content = []
            for blk in content:
                if (isinstance(blk, dict)
                        and blk.get("type") == "input_image"
                        and "image_url" in blk):
                    aid = image_id_resolver(blk["image_url"])
                    if aid:
                        new_content.append({"type": "input_image",
                                            "attachment_id": aid})
                    else:
                        new_content.append(blk)
                else:
                    new_content.append(blk)
            item = {**item, "content": new_content}
        out.append(item)
    return out


def select_tools_for_run(attachment_ids, *, session_id: str):
    """Return tool list; conditionally appends `read_attachment` when at least
    one attachment is non-image. Image-only or empty → unchanged ALL_TOOLS."""
    rows = _fetch_attachments(attachment_ids, session_id)
    has_non_image = any(r["kind"] != "image" for r in rows)
    if has_non_image:
        from skills.attachments import read_attachment
        return list(ALL_TOOLS) + [read_attachment]
    return list(ALL_TOOLS)


def attachment_system_block(attachment_ids, *, session_id: str) -> str:
    """System-prompt suffix listing non-image attachments. Empty string when
    there are no non-image attachments."""
    rows = _fetch_attachments(attachment_ids, session_id)
    non_image = [r for r in rows if r["kind"] != "image"]
    if not non_image:
        return ""
    lines = ["The user attached the following files to their message:"]
    for r in non_image:
        size_kb = max(1, r["size_bytes"] // 1024)
        lines.append(f"- id={r['id']}, name=\"{r['filename']}\", "
                     f"kind={r['kind']}, size={size_kb} KB")
    lines.append("Use read_attachment(id) to inspect contents. "
                 "Image attachments are already visible — don't call this on them. "
                 "For kind=document, the response may include an `error` field "
                 "(e.g., empty_scanned, encrypted, timeout) — relay it to the user "
                 "in plain language in their own language.")
    return "\n".join(lines)


class AgentRunner:
    def __init__(self, conn: sqlite3.Connection, confirm_mgr=None):
        self._conn = conn
        # Caller (main.py) passes the SAME ConfirmManager instance the
        # /confirm endpoint resolves against. Constructing a per-run mgr
        # caused every POST /confirm to 409 because skills registered into
        # a different in-memory _pending dict than the endpoint read from.
        if confirm_mgr is None:
            from confirm import ConfirmManager as _CM
            confirm_mgr = _CM(conn)
        self._confirm_mgr = confirm_mgr
        # Session-scoped Wiki clients. Calls go through the gateway, so wiki
        # service restarts (new random port) are transparent to us.
        self._wiki_clients: dict[str, WikiClient] = {}

    def _wiki_client_for(self, session_id: str, user_id: str) -> WikiClient:
        if session_id not in self._wiki_clients:
            self._wiki_clients[session_id] = WikiClient(user_id=str(user_id))
        return self._wiki_clients[session_id]

    def _load_history(self, session_id: str) -> list:
        # Each _save_history row already stores the full cumulative snapshot
        # (stream.to_input_list()), so only the most recent row is meaningful —
        # concatenating earlier rows would replay every turn's prefix again and
        # double the history on each run.
        row = self._conn.execute(
            "SELECT content FROM messages WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
            (session_id,)
        ).fetchone()
        if not row:
            return []
        try:
            history = json.loads(row["content"])
            return history if isinstance(history, list) else []
        except (json.JSONDecodeError, KeyError):
            return []

    def _save_history(self, session_id: str, history: list) -> None:
        msg_id = str(uuid.uuid4())
        self._conn.execute(
            "INSERT INTO messages (id, session_id, role, content, created_at) VALUES (?,?,?,?,?)",
            (msg_id, session_id, "history", json.dumps(history), int(time.time()))
        )
        self._conn.execute(
            "UPDATE sessions SET updated_at=? WHERE id=?",
            (int(time.time()), session_id)
        )
        self._conn.commit()

    async def run(
        self,
        session_id: str,
        user_id: str,
        message: str,
        sink,
        provider_key: str,
        provider_url: str,
        model_name: str,
        *,
        provider_type: str = "other",
        thinking: "ThinkingConfig | None" = None,
        kind: str = "chat",
        chat_username: str = "",
        user_patterns: list | None = None,
        run_id: str = "",
        attachment_ids: list[str] = (),
        context_photo=None,
    ) -> None:
        lock = _get_lock(session_id)
        if lock.locked():
            raise RuntimeError("agent_busy")

        async with lock:
            # `sink` is anything with an async `put(event)`. Today that's a
            # RunSink (persists+pubsubs); skills don't care about the type.
            APP_SESSION_VAR.set(session_id)
            APP_EVENT_VAR.set(sink)
            APP_CONFIRM_VAR.set(self._confirm_mgr)
            mb_skills.SESSION_ID_VAR.set(session_id)
            mb_skills.EVENT_QUEUE_VAR.set(sink)
            mb_skills.CONFIRM_MGR_VAR.set(self._confirm_mgr)

            # NEW for filesystem tools
            fs_skills.SESSION_ID_VAR.set(session_id)
            fs_skills.RUN_ID_VAR.set(run_id)
            fs_skills.EVENT_QUEUE_VAR.set(sink)
            fs_skills.DB_VAR.set(self._conn)
            fs_skills.STORE_VAR.set(_SNAPSHOT_STORE)
            fs_skills.CHAT_USERNAME_VAR.set(chat_username)
            fs_skills.USER_PATTERNS_VAR.set(user_patterns or [])
            fs_skills.CONFIRM_MGR_VAR.set(self._confirm_mgr)
            fs_access_request.clear_denied_for_session(session_id)

            shell_skills.SESSION_ID_VAR.set(session_id)

            # --- Wiki integration ---
            wiki_client = self._wiki_client_for(session_id, user_id)
            if wiki_client is not None:
                wiki_client.reset_cache()  # turn-scoped: fresh tree per turn
            wiki_skills.WIKI_CLIENT_VAR.set(wiki_client)
            wiki_skills.CONFIRM_MGR_VAR.set(self._confirm_mgr)
            wiki_skills.SESSION_ID_VAR.set(session_id)
            wiki_skills.EVENT_QUEUE_VAR.set(sink)
            wiki_skills.USER_PATTERNS_VAR.set(user_patterns or [])

            # Skills registry: tells list_skills() which user's runtime view to scan.
            skills_registry.SKILLS_ROOT_VAR.set(os.environ.get(
                "NIMOOS_SKILLS_ROOT", "/var/lib/nimoos/ai/skills"))
            skills_registry.USER_ID_VAR.set(str(user_id))

            # Search tools resolve the caller's accessible Wiki Roots from this
            # user_id (sent as X-NimoOS-User-ID). Without it, search returns
            # no_accessible_roots → empty hits. Per-skill var, matching the
            # established pattern (see spec 2026-05-29; unified context is a
            # tracked follow-up).
            search_skills.USER_ID_VAR.set(str(user_id))

            # Mount the user's skill runtime view into the bwrap sandbox via
            # ContextVar (not os.environ, which would be clobbered by concurrent
            # async requests in the same process — Fix 1.1).
            skills_root = os.environ.get("NIMOOS_SKILLS_ROOT", "/var/lib/nimoos/ai/skills")
            runtime_view = os.path.join(skills_root, ".runtime", str(user_id))
            if os.path.isdir(runtime_view):
                shell_skills.SANDBOX_SKILLS_VAR.set(runtime_view)
            # SANDBOX_SHELL_ROOT_VAR stays default (real persistent work dir for chat).

            client = AsyncOpenAI(base_url=provider_url, api_key=provider_key)
            # `should_replay_reasoning_content` lets the SDK inject prior
            # `reasoning_content` back onto assistant messages when replaying
            # history. DeepSeek thinking-mode (deepseek-v4-flash, deepseek-reasoner)
            # rejects requests where this field is missing on assistant turns
            # that originally produced it. The SDK ships a default policy that
            # handles DeepSeek correctly; not passing it (default None) means
            # no replay, which is why those models hit "reasoning_content must
            # be passed back" 400s mid-conversation.
            try:
                pt = ProviderType(provider_type)
            except ValueError:
                pt = ProviderType.OTHER
            model_settings = build_model_settings(pt, thinking)

            model = OpenAIChatCompletionsModel(
                model=model_name,
                openai_client=client,
                should_replay_reasoning_content=default_should_replay_reasoning_content,
            )

            base = init_doc.INIT_SYSTEM_PROMPT if kind == "init" else SYSTEM_PROMPT

            # Prepend the Wiki context block. 5s budget: if Wiki is slow we'd
            # rather drop the block than stall the user's chat.
            wiki_block = ""
            if wiki_client is not None:
                try:
                    wiki_block = await asyncio.wait_for(
                        WikiContextBuilder(wiki_client).build(user_patterns or []),
                        timeout=5.0,
                    )
                except Exception:
                    wiki_block = ""
            base_with_wiki = (wiki_block + "\n\n" + base) if wiki_block else base
            full_prompt = _compose_system_prompt(self._conn, session_id, base_with_wiki)

            if attachment_ids:
                att_block = attachment_system_block(attachment_ids, session_id=session_id)
                if att_block:
                    full_prompt = full_prompt + "\n\n" + att_block

            data_root = os.environ.get(
                "NIMOOS_AGENT_DATA_ROOT",
                str(db_module._DB_PATH.parent),
            )

            # Set context vars for the conditional read_attachment skill before
            # tool selection / Agent construction so tool invocations during
            # this run resolve the right session.
            from skills.attachments import (
                SESSION_ID_VAR as _ATT_S,
                USER_ID_VAR as _ATT_U,
                MAX_CHARS_VAR as _ATT_C,
                DATA_ROOT_VAR as _ATT_D,
            )
            _ATT_S.set(session_id)
            _ATT_U.set(user_id)
            _ATT_C.set(int(os.environ.get(
                "NIMOOS_MAX_ATTACHMENT_TEXT_CHARS", "32768")))
            _ATT_D.set(data_root)

            if context_photo is not None:
                parts = [f'[Viewing photo: "{context_photo.name}"']
                if context_photo.takenAt:
                    parts.append(f"taken {context_photo.takenAt}")
                if context_photo.place:
                    parts.append(f"location: {context_photo.place}")
                full_prompt += "\n\n" + ", ".join(parts) + "]"

            # model_settings belongs on Agent, NOT on OpenAIChatCompletionsModel —
            # the SDK constructor only takes (model, openai_client,
            # should_replay_reasoning_content). The Runner pulls model_settings
            # off Agent and threads it into each call.
            agent = Agent(
                name="NimoOS Agent",
                instructions=full_prompt,
                tools=select_tools_for_run(attachment_ids, session_id=session_id),
                model=model,
                model_settings=model_settings,
            )

            user_content = build_user_content(
                message, attachment_ids,
                session_id=session_id, data_root=data_root,
                model_name=model_name, provider_type=provider_type)
            history = self._load_history(session_id)
            # Earlier turns' image blocks were stored in compact form
            # (attachment_id only) to keep the DB small. Re-inline the base64
            # data URL before re-feeding to the SDK — the chat-completions
            # adapter rejects the compact shape with "Only image URLs are
            # supported for input_image".
            history = hydrate_image_blocks(
                history, session_id=session_id, data_root=data_root)
            input_messages = history + [{"role": "user", "content": user_content}]
            input_messages = _inject_synthetic_reasoning(input_messages)

            try:
                stream = Runner.run_streamed(agent, input_messages)
                # Maps tool call_id -> tool name so tool_result events can
                # report which tool produced the output (the SDK's output item
                # only carries call_id, not the name).
                call_names: dict[str, str] = {}
                # Per-run scratch shared with _convert_event:
                #   streamed_message — True once any message_delta is emitted.
                #     Used to suppress the SDK's final consolidated
                #     message_output_item (it would duplicate the streamed text).
                conv_state: dict = {"streamed_message": False}
                message_emitted = False  # any user-visible message text reached the client
                t_start = time.monotonic()
                t_first_token: float | None = None
                output_bytes = 0
                FIRST_ACTIVITY_TYPES = frozenset({"message_delta", "thinking", "tool_call"})
                BYTE_COUNT_TYPES = frozenset({"message_delta", "thinking"})

                async for event in stream.stream_events():
                    sse_event = _convert_event(event, call_names, conv_state)
                    if sse_event is None:
                        continue
                    et = sse_event["type"]
                    if et in FIRST_ACTIVITY_TYPES and t_first_token is None:
                        t_first_token = time.monotonic()
                    if et in BYTE_COUNT_TYPES:
                        content = sse_event.get("content")
                        if isinstance(content, str):
                            output_bytes += len(content.encode("utf-8"))
                    if et == "message_delta":
                        message_emitted = True
                    elif et == "message":
                        if conv_state["streamed_message"]:
                            continue
                        message_emitted = True
                    await sink.put(sse_event)

                # Reasoning-only fallback. The fallback text also counts toward
                # output_bytes so the token count is meaningful for these models.
                if not message_emitted:
                    final = getattr(stream, "final_output", None)
                    if final and isinstance(final, str) and final.strip():
                        await sink.put({"type": "message", "content": final})
                        output_bytes += len(final.encode("utf-8"))

                # stats_final — decoupled token count from timing:
                # output_tokens needs only bytes; tok/s and ttft need first-token.
                t_end = time.monotonic()
                total_ms = int((t_end - t_start) * 1000)
                output_tokens = (
                    max(1, round(output_bytes / 3)) if output_bytes > 0 else None
                )
                if t_first_token is not None:
                    ttft_ms = int((t_first_token - t_start) * 1000)
                    generation_ms = int((t_end - t_first_token) * 1000)
                    tokens_per_sec = (
                        round(output_tokens * 1000 / generation_ms, 1)
                        if output_tokens is not None and generation_ms > 0 else None
                    )
                else:
                    ttft_ms = None
                    generation_ms = None
                    tokens_per_sec = None

                await sink.put({
                    "type": "stats_final",
                    "ttft_ms": ttft_ms,
                    "generation_ms": generation_ms,
                    "total_ms": total_ms,
                    "output_tokens": output_tokens,
                    "tokens_per_sec": tokens_per_sec,
                    "source": "client_estimate",
                })

                final_history = stream.to_input_list()
                url_to_aid: dict[str, str] = {}
                if attachment_ids:
                    for r in _fetch_attachments(attachment_ids, session_id):
                        if r["kind"] != "image":
                            continue
                        full = os.path.join(
                            data_root, "sessions", session_id, "attachments",
                            r["rel_path"])
                        try:
                            with open(full, "rb") as f:
                                data = f.read()
                            url = (
                                f"data:{r['mime']};base64,"
                                f"{base64.b64encode(data).decode('ascii')}"
                            )
                            url_to_aid[url] = r["id"]
                        except FileNotFoundError:
                            pass
                final_history = compact_image_blocks(
                    final_history,
                    image_id_resolver=lambda u: url_to_aid.get(u))
                self._save_history(session_id, final_history)
            except Exception as e:
                await sink.put({"type": "error", "content": str(e)})
            finally:
                await sink.put({"type": "done"})


def _inject_synthetic_reasoning(items: list) -> list:
    """Insert a placeholder reasoning item before any assistant message that
    isn't already preceded by one.

    DeepSeek thinking-mode rejects requests where any prior assistant turn is
    missing `reasoning_content`. The Agents SDK only fills that field when a
    reasoning item directly precedes the message; if the SDK didn't capture
    one (which happens occasionally for short summary turns), the resulting
    chat-completions message has no reasoning_content and the API returns 400.
    A non-empty placeholder summary keeps the conversation valid without
    pretending the model "thought" anything specific.
    """
    if not isinstance(items, list):
        return items
    out: list = []
    for it in items:
        is_assistant_msg = (
            isinstance(it, dict)
            and it.get("type") == "message"
            and it.get("role") == "assistant"
        )
        if is_assistant_msg:
            prev = out[-1] if out else None
            prev_is_reasoning = (
                isinstance(prev, dict) and prev.get("type") == "reasoning"
            )
            if not prev_is_reasoning:
                out.append({
                    "type": "reasoning",
                    "id": "__synthetic__",
                    "summary": [{
                        "type": "summary_text",
                        "text": "(no reasoning captured for this turn)",
                    }],
                    "provider_data": {"model": "deepseek-synthetic"},
                })
        out.append(it)
    return out


def _raw_attr(obj, key, default=None):
    """Read a field from either a Pydantic model or a dict raw_item."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _convert_event(event, call_names: dict[str, str] | None = None,
                   state: dict | None = None) -> dict | None:
    """Translate one SDK stream event to an SSE event dict.

    `state` is per-run scratch shared with the run loop:
      - state["streamed_message"] flips True when we emit any message_delta,
        so the run loop knows to suppress the final consolidated
        message_output_item (which would otherwise duplicate the streamed text).
    """
    if call_names is None:
        call_names = {}
    if state is None:
        state = {}
    try:
        from agents.stream_events import RawResponsesStreamEvent, RunItemStreamEvent
        if isinstance(event, RawResponsesStreamEvent):
            data = event.data

            # Chat Completions streaming format. delta.content is the actual
            # response text — tag it as message (streamed). delta.reasoning_content
            # (DeepSeek-R1 / o1-style models) is the chain-of-thought — tag as thinking.
            if hasattr(data, "choices") and data.choices:
                delta = data.choices[0].delta
                reasoning = (
                    getattr(delta, "reasoning_content", None)
                    or getattr(delta, "reasoning", None)
                )
                if reasoning:
                    return {"type": "thinking", "content": reasoning}
                content = getattr(delta, "content", None)
                if content:
                    state["streamed_message"] = True
                    return {"type": "message_delta", "content": content}
                return None

            # Responses API streaming format. The event class name discriminates
            # between reasoning-summary deltas and output-text deltas; fall back
            # to tagging as message when ambiguous (better than mis-classifying
            # the response as reasoning, which is what the prior code did).
            delta = getattr(data, "delta", None)
            if isinstance(delta, str) and delta:
                cls_name = type(data).__name__.lower()
                if "reasoning" in cls_name:
                    return {"type": "thinking", "content": delta}
                state["streamed_message"] = True
                return {"type": "message_delta", "content": delta}

        elif isinstance(event, RunItemStreamEvent):
            item = event.item
            item_type = getattr(item, "type", None)

            # Normalise: SDK uses both 'message_output_item' and 'message'
            if item_type in ("message_output_item", "message"):
                content = ""
                for block in getattr(item, "content", []):
                    block_type = getattr(block, "type", "")
                    if block_type in ("output_text", "text"):
                        content += getattr(block, "text", "")
                if not content:
                    # Fallback: raw text attribute
                    content = getattr(item, "text", "") or getattr(item, "output", "")
                if content:
                    return {"type": "message", "content": str(content)}

            # Tool call — extract name + arguments from raw_item
            # (SDK's RunItem wraps a ResponseFunctionToolCall / dict).
            if item_type in ("tool_call_item", "function_call"):
                raw = getattr(item, "raw_item", None)
                name = (
                    getattr(item, "title", None)
                    or _raw_attr(raw, "name")
                    or _raw_attr(raw, "call_id")
                    or ""
                )
                args_raw = _raw_attr(raw, "arguments", "")
                # arguments is a JSON string in OpenAI tool-call format
                args: dict = {}
                if isinstance(args_raw, str) and args_raw:
                    try:
                        parsed = json.loads(args_raw)
                        if isinstance(parsed, dict):
                            args = parsed
                        else:
                            args = {"_": parsed}
                    except json.JSONDecodeError:
                        args = {"_raw": args_raw}
                elif isinstance(args_raw, dict):
                    args = args_raw

                call_id = _raw_attr(raw, "call_id") or _raw_attr(raw, "id")
                if call_id and name:
                    call_names[call_id] = name

                return {
                    "type": "tool_call",
                    "tool": name,
                    "args": args,
                    "call_id": call_id or "",
                }

            # Tool result — output item only carries call_id, so look up the
            # tool name in the map populated when the matching tool_call event
            # was emitted earlier in the run.
            if item_type in ("tool_call_output_item", "function_call_output"):
                raw = getattr(item, "raw_item", None)
                call_id = _raw_attr(raw, "call_id") or _raw_attr(raw, "id")
                tool_name = call_names.get(call_id, "") if call_id else ""
                output = getattr(item, "output", None)
                if output is None:
                    output = _raw_attr(raw, "output", "")
                return {
                    "type": "tool_result",
                    "tool": tool_name,
                    "content": str(output) if output is not None else "",
                    "call_id": call_id or "",
                }

    except Exception:
        pass
    return None
