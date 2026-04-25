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
from confirm import ConfirmManager
from skills import ALL_TOOLS
from skills.app_management import SESSION_ID_VAR, EVENT_QUEUE_VAR, CONFIRM_MGR_VAR
import skills.message_bus as mb_skills

SYSTEM_PROMPT = """You are a NimoOS NAS management assistant.
You help users manage their NAS system: applications, storage, services, and message bus actions.
For read operations, you can act immediately.
For write operations (install, start, stop, restart, uninstall, update, trigger),
you must call the tool — the system will prompt the user for confirmation automatically.
Always explain what you found or did in clear, concise language."""

_session_locks: dict[str, asyncio.Lock] = {}


def _get_lock(session_id: str) -> asyncio.Lock:
    if session_id not in _session_locks:
        _session_locks[session_id] = asyncio.Lock()
    return _session_locks[session_id]


class AgentRunner:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def _load_history(self, session_id: str) -> list:
        rows = self._conn.execute(
            "SELECT content FROM messages WHERE session_id=? ORDER BY created_at",
            (session_id,)
        ).fetchall()
        result = []
        for row in rows:
            try:
                result.extend(json.loads(row["content"]))
            except (json.JSONDecodeError, KeyError):
                pass
        return result

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
        queue: asyncio.Queue,
        provider_key: str,
        provider_url: str,
        model_name: str,
    ) -> None:
        lock = _get_lock(session_id)
        if lock.locked():
            raise RuntimeError("agent_busy")

        async with lock:
            confirm_mgr = ConfirmManager(self._conn)

            SESSION_ID_VAR.set(session_id)
            EVENT_QUEUE_VAR.set(queue)
            CONFIRM_MGR_VAR.set(confirm_mgr)
            mb_skills.SESSION_ID_VAR.set(session_id)
            mb_skills.EVENT_QUEUE_VAR.set(queue)
            mb_skills.CONFIRM_MGR_VAR.set(confirm_mgr)

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
                # Runner.run_streamed returns RunResultStreaming directly (not a context manager)
                stream = Runner.run_streamed(agent, input_messages)
                async for event in stream.stream_events():
                    sse_event = _convert_event(event)
                    if sse_event:
                        await queue.put(sse_event)
                self._save_history(session_id, stream.to_input_list())
            finally:
                await queue.put({"type": "done"})


def _convert_event(event) -> dict | None:
    try:
        from agents.stream_events import RawResponsesStreamEvent, RunItemStreamEvent
        if isinstance(event, RawResponsesStreamEvent):
            data = event.data
            if hasattr(data, "choices") and data.choices:
                delta = data.choices[0].delta
                if hasattr(delta, "content") and delta.content:
                    return {"type": "thinking", "content": delta.content}
        elif isinstance(event, RunItemStreamEvent):
            item = event.item
            if hasattr(item, "type"):
                if item.type == "tool_call_item":
                    return {
                        "type": "tool_call",
                        "tool": getattr(item, "name", ""),
                        "args": {},
                    }
                if item.type == "tool_call_output_item":
                    return {
                        "type": "tool_result",
                        "tool": "",
                        "content": str(getattr(item, "output", "")),
                    }
                if item.type == "message_output_item":
                    content = ""
                    for block in getattr(item, "content", []):
                        if getattr(block, "type", "") == "output_text":
                            content += block.text
                    if content:
                        return {"type": "message", "content": content}
    except (ImportError, AttributeError):
        pass
    return None
