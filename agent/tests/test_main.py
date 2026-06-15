import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch, AsyncMock

@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "test.db"))
    import importlib
    import sys
    # Clean up any cached modules
    for mod in ["main", "agent", "db"]:
        sys.modules.pop(mod, None)
    import main
    importlib.reload(main)
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as c:
        yield c

@pytest.mark.asyncio
async def test_health_returns_ok(client):
    resp = await client.get("/agent/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

@pytest.mark.asyncio
async def test_healthz_ok(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

@pytest.mark.asyncio
async def test_create_session(client):
    resp = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    assert resp.status_code == 200
    data = resp.json()
    assert "session_id" in data

@pytest.mark.asyncio
async def test_list_sessions(client):
    await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    resp = await client.get("/agent/sessions", headers={"X-User-Id": "1"})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)

@pytest.mark.asyncio
async def test_delete_session(client):
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    session_id = r.json()["session_id"]
    resp = await client.delete(f"/agent/sessions/{session_id}", headers={"X-User-Id": "1"})
    assert resp.status_code == 200

@pytest.mark.asyncio
async def test_run_missing_user_id_returns_401(client):
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    session_id = r.json()["session_id"]
    resp = await client.post(f"/agent/sessions/{session_id}/run", json={"message": "hi"})
    assert resp.status_code == 401

@pytest.mark.asyncio
async def test_confirm_missing_id_returns_400(client):
    resp = await client.post(
        "/agent/sessions/nonexistent/confirm",
        headers={"X-User-Id": "1"},
        json={"confirmed": True},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_confirm_unknown_id_returns_409(client):
    resp = await client.post(
        "/agent/sessions/nonexistent/confirm",
        headers={"X-User-Id": "1"},
        json={"confirm_id": "does-not-exist", "confirmed": True},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_cancel_no_active_run_is_idempotent(client):
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    session_id = r.json()["session_id"]
    resp = await client.post(
        f"/agent/sessions/{session_id}/cancel",
        headers={"X-User-Id": "1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_cancelled"] is False


@pytest.mark.asyncio
async def test_cancel_running_task_releases_lock(client):
    """Verify that after /cancel, the next /run isn't rejected with agent_busy
    because the lock from the cancelled task has been released."""
    import sys
    main = sys.modules["main"]
    import asyncio as _asyncio
    from run_sink import RunSink as _RunSink

    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    session_id = r.json()["session_id"]

    # Simulate an in-flight agent task that's holding the per-session lock,
    # without actually running an LLM. The lock comes from agent.py's
    # _session_locks; acquiring it inside the fake task is enough to make a
    # follow-up _runner.run() raise 'agent_busy' without cancellation.
    from agent import _get_lock
    lock = _get_lock(session_id)

    sink = _RunSink("fake-run-id", session_id, main._conn)
    main._active_runs[session_id] = sink

    async def fake_agent_task():
        async with lock:
            try:
                # Block forever, mimicking a long agent run.
                await _asyncio.Event().wait()
            except _asyncio.CancelledError:
                # Surface a clean termination like the real wrapper does.
                await sink.put({"type": "error", "content": "已停止"})
                await sink.put({"type": "done"})
                raise

    task = _asyncio.create_task(fake_agent_task())
    sink.task = task
    # Yield once so the task acquires the lock before we test cancel.
    await _asyncio.sleep(0)
    assert lock.locked()

    resp = await client.post(
        f"/agent/sessions/{session_id}/cancel",
        headers={"X-User-Id": "1"},
    )
    assert resp.status_code == 200
    assert resp.json()["task_cancelled"] is True

    # Lock must be released now — that's the whole point of the cancel path.
    assert not lock.locked()


@pytest.mark.asyncio
async def test_run_stream_no_run_returns_204(client):
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    session_id = r.json()["session_id"]
    resp = await client.get(
        f"/agent/sessions/{session_id}/run-stream",
        headers={"X-User-Id": "1"},
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_run_stream_session_not_found(client):
    resp = await client.get(
        "/agent/sessions/nonexistent/run-stream",
        headers={"X-User-Id": "1"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_run_stream_replays_errored_run(client, tmp_path):
    # Build a session + finished-with-error run in the DB so /run-stream takes
    # the replay-from-event-log branch.
    import sys
    main = sys.modules["main"]

    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    session_id = r.json()["session_id"]

    import uuid as _uuid, time as _time, json as _json
    run_id = str(_uuid.uuid4())
    main._conn.execute(
        "INSERT INTO agent_runs (id, session_id, user_id, status, user_message, created_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (run_id, session_id, "1", "error", "tell me a joke",
         int(_time.time()) - 10, int(_time.time())),
    )
    for seq, payload in enumerate([
        {"type": "thinking", "content": "thinking..."},
        {"type": "error", "content": "boom"},
        {"type": "done"},
    ], start=1):
        main._conn.execute(
            "INSERT INTO event_log (run_id, seq, payload, created_at) VALUES (?,?,?,?)",
            (run_id, seq, _json.dumps(payload), int(_time.time())),
        )
    main._conn.commit()

    async with client.stream(
        "GET", f"/agent/sessions/{session_id}/run-stream",
        headers={"X-User-Id": "1"},
    ) as resp:
        assert resp.status_code == 200
        body = b""
        async for chunk in resp.aiter_bytes():
            body += chunk
        text = body.decode()

    # Synthetic prefix has the user_message; replay carries the thinking/error/done.
    assert '"type": "user_message"' in text
    assert "tell me a joke" in text
    assert '"type": "thinking"' in text
    assert '"type": "error"' in text
    assert '"type": "done"' in text
