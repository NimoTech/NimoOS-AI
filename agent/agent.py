import asyncio
import json
import sqlite3
import time
import uuid
from typing import AsyncIterator

from agents import Agent, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

import db as db_module
from skills import ALL_TOOLS
from skills.app_management import SESSION_ID_VAR, EVENT_QUEUE_VAR, CONFIRM_MGR_VAR
import skills.message_bus as mb_skills

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
- Match the user's language. Be concise by default; expand when the task warrants it."""

_session_locks: dict[str, asyncio.Lock] = {}


def _get_lock(session_id: str) -> asyncio.Lock:
    if session_id not in _session_locks:
        _session_locks[session_id] = asyncio.Lock()
    return _session_locks[session_id]


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
    ) -> None:
        lock = _get_lock(session_id)
        if lock.locked():
            raise RuntimeError("agent_busy")

        async with lock:
            # `sink` is anything with an async `put(event)`. Today that's a
            # RunSink (persists+pubsubs); skills don't care about the type.
            SESSION_ID_VAR.set(session_id)
            EVENT_QUEUE_VAR.set(sink)
            CONFIRM_MGR_VAR.set(self._confirm_mgr)
            mb_skills.SESSION_ID_VAR.set(session_id)
            mb_skills.EVENT_QUEUE_VAR.set(sink)
            mb_skills.CONFIRM_MGR_VAR.set(self._confirm_mgr)

            client = AsyncOpenAI(base_url=provider_url, api_key=provider_key)
            model = OpenAIChatCompletionsModel(model=model_name, openai_client=client)

            agent = Agent(
                name="NimoOS Agent",
                instructions=SYSTEM_PROMPT,
                tools=ALL_TOOLS,
                model=model,
            )

            history = self._load_history(session_id)
            input_messages = history + [{"role": "user", "content": message}]

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

                async for event in stream.stream_events():
                    sse_event = _convert_event(event, call_names, conv_state)
                    if sse_event is None:
                        continue
                    if sse_event["type"] == "message_delta":
                        message_emitted = True
                    elif sse_event["type"] == "message":
                        # End-of-turn consolidated text from message_output_item.
                        # Drop if we already streamed the same content via deltas.
                        if conv_state["streamed_message"]:
                            continue
                        message_emitted = True
                    await sink.put(sse_event)

                # Reasoning-only models (e.g. deepseek-v4-flash) emit the full
                # answer as reasoning_content with no content delta and no
                # message_output_item — fall back to final_output so the user
                # sees something.
                if not message_emitted:
                    final = getattr(stream, "final_output", None)
                    if final and isinstance(final, str) and final.strip():
                        await sink.put({"type": "message", "content": final})

                self._save_history(session_id, stream.to_input_list())
            except Exception as e:
                await sink.put({"type": "error", "content": str(e)})
            finally:
                await sink.put({"type": "done"})


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
                }

    except Exception:
        pass
    return None
