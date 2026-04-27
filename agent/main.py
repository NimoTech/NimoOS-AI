import asyncio
import json
import os
import time
import uuid
from typing import AsyncGenerator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

import db as db_module
from agent import AgentRunner
from confirm import ConfirmManager
from openai import AsyncOpenAI
import title_gen

_DB_PATH = os.environ.get("AGENT_DB_PATH", str(db_module._DB_PATH))
_conn = db_module.init_db(_DB_PATH)
_runner = AgentRunner(_conn)
_confirm_mgr = ConfirmManager(_conn)

# Per-session SSE queues
_session_queues: dict[str, asyncio.Queue] = {}

app = FastAPI(title="nimoos-agent")


class RunRequest(BaseModel):
    message: str
    model: str = "gpt-4o-mini"


class ConfirmRequest(BaseModel):
    confirmed: bool = True

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


@app.post("/agent/sessions")
async def create_session(x_user_id: str = Header(..., alias="X-User-Id")):
    session_id = str(uuid.uuid4())
    now = int(time.time())
    _conn.execute(
        "INSERT INTO sessions (id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
        (session_id, x_user_id, None, now, now)
    )
    _conn.commit()
    return {"session_id": session_id}


@app.get("/agent/sessions")
async def list_sessions(x_user_id: str = Header(..., alias="X-User-Id")):
    rows = _conn.execute(
        "SELECT id, title, created_at, updated_at FROM sessions WHERE user_id=? ORDER BY updated_at DESC",
        (x_user_id,)
    ).fetchall()
    return [dict(row) for row in rows]


@app.delete("/agent/sessions/{session_id}")
async def delete_session(session_id: str, x_user_id: str = Header(..., alias="X-User-Id")):
    _conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    _conn.execute("DELETE FROM sessions WHERE id=? AND user_id=?", (session_id, x_user_id))
    _conn.commit()
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

    return _hydrate_messages(history)


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


def _hydrate_messages(history: list) -> list:
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
            text = _flatten_content(item.get("content"))
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
                max_tokens=40,
            ),
            timeout=15.0,
        )
        raw = resp.choices[0].message.content if resp.choices else ""
        cleaned = title_gen.clean_llm_title(raw or "")
        if not cleaned:
            return _persist(fallback_title, True)
        return _persist(cleaned, False)
    except (asyncio.TimeoutError, Exception):
        return _persist(fallback_title, True)


@app.post("/agent/sessions/{session_id}/run")
async def run_session(
    session_id: str,
    req: RunRequest,
    x_user_id: str = Header(..., alias="X-User-Id"),
    x_agent_provider_key: str = Header(..., alias="X-Agent-Provider-Key"),
    x_agent_provider_url: str = Header(..., alias="X-Agent-Provider-Url"),
):
    row = _conn.execute("SELECT id FROM sessions WHERE id=? AND user_id=?",
                        (session_id, x_user_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="session not found")

    queue: asyncio.Queue = asyncio.Queue()
    _session_queues[session_id] = queue

    async def run_agent():
        try:
            await _runner.run(
                session_id, x_user_id, req.message, queue,
                x_agent_provider_key, x_agent_provider_url, req.model,
            )
        except RuntimeError as e:
            if "agent_busy" in str(e):
                await queue.put({"type": "error", "content": "Agent is processing a previous message. Please wait."})
            else:
                await queue.put({"type": "error", "content": str(e)})
            await queue.put({"type": "done"})
        except Exception as e:
            await queue.put({"type": "error", "content": str(e)})
            await queue.put({"type": "done"})

    asyncio.create_task(run_agent())

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=120)
                except asyncio.TimeoutError:
                    yield 'data: {"type": "error", "content": "timeout"}\n\n'
                    yield 'data: {"type": "done"}\n\n'
                    break
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") == "done":
                    break
        finally:
            _session_queues.pop(session_id, None)

    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})


@app.post("/agent/sessions/{session_id}/confirm")
async def confirm_session(
    session_id: str,
    request: Request,
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    try:
        body = await request.body()
        confirmed = True
        if body:
            try:
                import json as _json
                data = _json.loads(body)
                confirmed = bool(data.get("confirmed", True))
            except Exception:
                pass
        _confirm_mgr.resolve(session_id, confirmed)
    except KeyError:
        raise HTTPException(status_code=409, detail="session_expired")
    return {"ok": True}


@app.post("/agent/sessions/{session_id}/cancel")
async def cancel_session(
    session_id: str,
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    try:
        _confirm_mgr.resolve(session_id, confirmed=False)
    except KeyError:
        raise HTTPException(status_code=409, detail="session_expired")
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8282)
