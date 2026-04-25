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
                await queue.put({"type": "done"})
            else:
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
