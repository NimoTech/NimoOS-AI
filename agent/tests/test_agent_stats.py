"""Verify AgentRunner emits a single stats_final SSE event before done,
using client-side wallclock + utf8_bytes/3 token estimation."""
import asyncio
import time
import uuid
from unittest.mock import MagicMock

import pytest

import db as db_module
from agent import AgentRunner


@pytest.fixture
def conn(tmp_path):
    return db_module.init_db(str(tmp_path / "test.db"))


@pytest.fixture
def runner(conn):
    return AgentRunner(conn)


def _insert_session(conn, session_id: str) -> None:
    conn.execute(
        "INSERT INTO sessions (id, user_id, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (session_id, "u", "t", int(time.time()), int(time.time())),
    )
    conn.commit()


async def _drain(queue: asyncio.Queue) -> list[dict]:
    out: list[dict] = []
    while True:
        try:
            out.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return out


def _stub_stream(events_to_yield: list, final_output: str | None = None) -> MagicMock:
    """Build a fake RunResultStreaming whose stream_events yields the given
    raw markers (we patch _convert_event to translate them)."""
    async def stream_events():
        for r in events_to_yield:
            await asyncio.sleep(0.01)
            yield r

    mock = MagicMock()
    mock.stream_events = stream_events
    mock.to_input_list.return_value = []
    mock.final_output = final_output
    return mock


@pytest.mark.asyncio
async def test_stats_final_message_delta_path(runner, conn, monkeypatch):
    """Three message_delta events: stats_final has ttft, generation, token count,
    tok/s — all populated from wallclock + utf8 byte count."""
    session_id = str(uuid.uuid4())
    _insert_session(conn, session_id)
    queue: asyncio.Queue = asyncio.Queue()

    seq = iter([
        {"type": "message_delta", "content": "hello "},   # 6 bytes
        {"type": "message_delta", "content": "world!"},   # 6 bytes
        {"type": "message_delta", "content": " 你好"},    # 1 + 3 + 3 = 7 bytes
    ])

    def fake_convert(event, call_names=None, state=None):
        if state is not None:
            state["streamed_message"] = True
        return next(seq)

    monkeypatch.setattr(
        "agent.Runner.run_streamed",
        lambda *a, **k: _stub_stream([object(), object(), object()]),
    )
    monkeypatch.setattr("agent._convert_event", fake_convert)

    await runner.run(session_id, "u", "hi", queue,
                     "k", "http://localhost/v1", "gpt-4o-mini")

    events = await _drain(queue)
    types = [e["type"] for e in events]
    assert types[-2:] == ["stats_final", "done"]
    # No stats_first_token in the wire protocol anymore.
    assert "stats_first_token" not in types

    final = next(e for e in events if e["type"] == "stats_final")
    # 6 + 6 + 7 = 19 bytes; round(19 / 3) = 6 tokens.
    assert final["output_tokens"] == 6
    assert isinstance(final["ttft_ms"], int) and final["ttft_ms"] >= 0
    assert isinstance(final["generation_ms"], int) and final["generation_ms"] >= 0
    assert final["total_ms"] >= final["generation_ms"]
    assert final["tokens_per_sec"] is not None and final["tokens_per_sec"] > 0
    assert final["source"] == "client_estimate"


@pytest.mark.asyncio
async def test_stats_first_token_triggered_by_thinking(runner, conn, monkeypatch):
    """Reasoning models emit `thinking` before `message_delta`. ttft must be
    measured from the first thinking event, and thinking content must count
    toward output_tokens."""
    session_id = str(uuid.uuid4())
    _insert_session(conn, session_id)
    queue: asyncio.Queue = asyncio.Queue()

    seq = iter([
        {"type": "thinking", "content": "let me think..."},  # 15 bytes
        {"type": "thinking", "content": " more"},            # 5 bytes
        {"type": "message_delta", "content": "Answer."},     # 7 bytes
    ])

    def fake_convert(event, call_names=None, state=None):
        ev = next(seq)
        if state is not None and ev["type"] == "message_delta":
            state["streamed_message"] = True
        return ev

    monkeypatch.setattr(
        "agent.Runner.run_streamed",
        lambda *a, **k: _stub_stream([object(), object(), object()]),
    )
    monkeypatch.setattr("agent._convert_event", fake_convert)

    await runner.run(session_id, "u", "hi", queue,
                     "k", "http://localhost/v1", "deepseek-r1")

    final = next(e for e in await _drain(queue) if e["type"] == "stats_final")
    # 15 + 5 + 7 = 27 bytes; round(27 / 3) = 9 tokens.
    assert final["output_tokens"] == 9
    assert final["ttft_ms"] is not None  # set by first thinking event


@pytest.mark.asyncio
async def test_stats_final_fallback_path_no_streamed_events(runner, conn, monkeypatch):
    """Reasoning-only path: no message_delta / thinking via convert, but
    stream.final_output is non-empty. output_tokens must be populated from
    final_output bytes; ttft and tok/s stay null."""
    session_id = str(uuid.uuid4())
    _insert_session(conn, session_id)
    queue: asyncio.Queue = asyncio.Queue()

    async def empty_events():
        return
        yield

    mock = MagicMock()
    mock.stream_events = empty_events
    mock.to_input_list.return_value = []
    mock.final_output = "answer in 6 bytes"  # 17 bytes
    monkeypatch.setattr("agent.Runner.run_streamed", lambda *a, **k: mock)

    await runner.run(session_id, "u", "hi", queue,
                     "k", "http://localhost/v1", "deepseek-r1")
    final = next(e for e in await _drain(queue) if e["type"] == "stats_final")

    # 17 bytes / 3 = 6 tokens (rounded).
    assert final["output_tokens"] == 6
    assert final["ttft_ms"] is None
    assert final["generation_ms"] is None
    assert final["tokens_per_sec"] is None
    assert isinstance(final["total_ms"], int) and final["total_ms"] >= 0


@pytest.mark.asyncio
async def test_stats_final_empty_run(runner, conn, monkeypatch):
    """No events, no fallback text: only total_ms is populated, all else null."""
    session_id = str(uuid.uuid4())
    _insert_session(conn, session_id)
    queue: asyncio.Queue = asyncio.Queue()

    async def empty_events():
        return
        yield

    mock = MagicMock()
    mock.stream_events = empty_events
    mock.to_input_list.return_value = []
    mock.final_output = None
    monkeypatch.setattr("agent.Runner.run_streamed", lambda *a, **k: mock)

    await runner.run(session_id, "u", "hi", queue,
                     "k", "http://localhost/v1", "gpt-4o-mini")
    final = next(e for e in await _drain(queue) if e["type"] == "stats_final")
    assert final["ttft_ms"] is None
    assert final["generation_ms"] is None
    assert final["output_tokens"] is None
    assert final["tokens_per_sec"] is None
    assert isinstance(final["total_ms"], int)


@pytest.mark.asyncio
async def test_no_stats_final_on_exception(runner, conn, monkeypatch):
    """Mid-stream exception → error + done, no stats_final."""
    session_id = str(uuid.uuid4())
    _insert_session(conn, session_id)
    queue: asyncio.Queue = asyncio.Queue()

    async def boom():
        raise RuntimeError("upstream blew up")
        yield

    mock = MagicMock()
    mock.stream_events = boom
    mock.to_input_list.return_value = []
    mock.final_output = None
    monkeypatch.setattr("agent.Runner.run_streamed", lambda *a, **k: mock)

    await runner.run(session_id, "u", "hi", queue,
                     "k", "http://localhost/v1", "gpt-4o-mini")
    types = [e["type"] for e in await _drain(queue)]
    assert "stats_final" not in types
    assert "error" in types
    assert types[-1] == "done"
