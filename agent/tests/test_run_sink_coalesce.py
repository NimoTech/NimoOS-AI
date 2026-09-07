"""RunSink persistence coalescing + event_log TTL sweep (P5 event flood)."""
import asyncio
import json
import sqlite3
import time

import pytest

import run_sink as rs
from db import init_db


def _db(tmp_path):
    return init_db(str(tmp_path / "s.db"))


def _rows(db, run_id):
    return [json.loads(r["payload"]) for r in
            db.execute("SELECT payload FROM event_log WHERE run_id=? ORDER BY seq", (run_id,))]


@pytest.mark.asyncio
async def test_consecutive_thinking_deltas_persist_as_one_row_but_fan_out_raw(tmp_path):
    db = _db(tmp_path)
    sink = rs.RunSink("r1", "s1", db)
    _, q = sink.subscribe()
    for ch in "abc":
        await sink.put({"type": "thinking", "content": ch})
    await sink.put({"type": "tool_call", "tool": "x", "args": {}, "call_id": "c"})
    await sink.put({"type": "done"})
    assert _rows(db, "r1") == [{"type": "thinking", "content": "abc"},
                               {"type": "tool_call", "tool": "x", "args": {}, "call_id": "c"},
                               {"type": "done"}]
    live = [q.get_nowait() for _ in range(5)]
    assert [e.get("content") for e in live[:3]] == ["a", "b", "c"]      # raw deltas live
    assert sink._past == _rows(db, "r1")
    assert sink.is_done


@pytest.mark.asyncio
async def test_type_change_and_message_delta_boundaries(tmp_path):
    db = _db(tmp_path)
    sink = rs.RunSink("r2", "s1", db)
    await sink.put({"type": "thinking", "content": "t1"})
    await sink.put({"type": "message_delta", "content": "m1"})
    await sink.put({"type": "message_delta", "content": "m2"})
    await sink.put({"type": "thinking", "content": "t2"})
    await sink.put({"type": "done"})
    assert [(e["type"], e["content"]) for e in _rows(db, "r2")[:-1]] == [
        ("thinking", "t1"), ("message_delta", "m1m2"), ("thinking", "t2")]


@pytest.mark.asyncio
async def test_subagent_inner_deltas_coalesce_per_parent_call(tmp_path):
    db = _db(tmp_path)
    sink = rs.RunSink("r3", "s1", db)
    def w(cid, t, c):
        return {"type": "subagent_event", "parent_call_id": cid, "depth": 1, "event": {"type": t, "content": c}}
    await sink.put(w("p1", "thinking", "a"))
    await sink.put(w("p1", "thinking", "b"))
    await sink.put(w("p2", "thinking", "X"))          # different child → its own scope buffer
    await sink.put(w("p1", "thinking", "c"))          # p1 keeps coalescing across the interleave
    await sink.put(w("p1", "tool_call", "ignored"))   # non-delta → flush every scope (insertion order), then own row
    await sink.put({"type": "done"})
    rows = _rows(db, "r3")
    assert [(r["parent_call_id"], r["event"]["type"], r["event"].get("content")) for r in rows[:-1]] == [
        ("p1", "thinking", "abc"), ("p2", "thinking", "X"), ("p1", "tool_call", "ignored")]


@pytest.mark.asyncio
async def test_original_event_objects_are_not_mutated(tmp_path):
    db = _db(tmp_path)
    sink = rs.RunSink("r4", "s1", db)
    first = {"type": "thinking", "content": "a"}
    inner_first = {"type": "subagent_event", "parent_call_id": "p", "depth": 1, "event": {"type": "thinking", "content": "x"}}
    await sink.put(first); await sink.put({"type": "thinking", "content": "b"})
    await sink.put(inner_first); await sink.put({"type": "subagent_event", "parent_call_id": "p", "depth": 1, "event": {"type": "thinking", "content": "y"}})
    sink.flush()
    assert first["content"] == "a" and inner_first["event"]["content"] == "x"
    assert _rows(db, "r4")[0]["content"] == "ab" and _rows(db, "r4")[1]["event"]["content"] == "xy"


@pytest.mark.asyncio
async def test_max_chars_splits_the_buffer(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "COALESCE_MAX_CHARS", 5)
    db = _db(tmp_path)
    sink = rs.RunSink("r5", "s1", db)
    for _ in range(4):
        await sink.put({"type": "thinking", "content": "xx"})
    sink.flush()
    assert [r["content"] for r in _rows(db, "r5")] == ["xxxx", "xxxx"]


@pytest.mark.asyncio
async def test_timer_flushes_open_buffer_after_max_age(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "FLUSH_INTERVAL", 0.05)
    db = _db(tmp_path)
    sink = rs.RunSink("r6", "s1", db)
    await sink.put({"type": "thinking", "content": "slow"})
    assert _rows(db, "r6") == []                       # buffered (max-age timer, not idle)
    await asyncio.sleep(0.12)
    assert _rows(db, "r6") == [{"type": "thinking", "content": "slow"}]
    await sink.put({"type": "thinking", "content": "again"})   # new buffer after a flush
    sink.flush()
    assert [r["content"] for r in _rows(db, "r6")] == ["slow", "again"]


@pytest.mark.asyncio
async def test_subscribe_flushes_so_late_joiner_sees_buffered_deltas(tmp_path):
    db = _db(tmp_path)
    sink = rs.RunSink("r7", "s1", db)
    await sink.put({"type": "thinking", "content": "buf"})
    past, q = sink.subscribe()
    assert past == [{"type": "thinking", "content": "buf"}]
    await sink.put({"type": "thinking", "content": "live"})
    assert q.get_nowait() == {"type": "thinking", "content": "live"}


@pytest.mark.asyncio
async def test_persist_failure_never_blocks_fanout(tmp_path):
    db = _db(tmp_path)
    sink = rs.RunSink("r8", "s1", db)
    db.close()                                          # every INSERT now raises
    _, q = sink.subscribe()
    await sink.put({"type": "tool_call", "tool": "x", "args": {}, "call_id": "c"})
    await sink.put({"type": "done"})
    assert q.qsize() == 2 and sink.is_done


@pytest.mark.asyncio
async def test_load_events_from_db_returns_coalesced_rows(tmp_path):
    db = _db(tmp_path)
    sink = rs.RunSink("r9", "s1", db)
    for ch in "hello":
        await sink.put({"type": "message_delta", "content": ch})
    await sink.put({"type": "done"})
    assert rs.load_events_from_db(db, "r9") == [{"type": "message_delta", "content": "hello"}, {"type": "done"}]


def test_sweep_event_log_deletes_only_expired_rows(tmp_path):
    db = _db(tmp_path)
    now = int(time.time())
    old, fresh = now - 40 * 86400, now - 2 * 86400
    db.execute("INSERT INTO event_log (run_id, seq, payload, created_at) VALUES ('a',1,'{}',?)", (old,))
    db.execute("INSERT INTO event_log (run_id, seq, payload, created_at) VALUES ('b',1,'{}',?)", (fresh,))
    db.commit()
    assert rs.sweep_event_log(db, ttl_days=30, now=now) == 1
    assert [r["run_id"] for r in db.execute("SELECT run_id FROM event_log")] == ["b"]
    assert rs.sweep_event_log(db, ttl_days=30, now=now) == 0
    db.close()
    assert rs.sweep_event_log(db) == 0                  # closed db → 0, never raises


@pytest.mark.asyncio
async def test_interleaved_streams_coalesce_per_scope(tmp_path):
    """Parent + 3 children streaming round-robin must still coalesce (one
    buffer per UI scope); non-delta events flush every scope in order."""
    db = _db(tmp_path)
    sink = rs.RunSink("r10", "s1", db)
    def child(cid, c):
        return {"type": "subagent_event", "parent_call_id": cid, "depth": 1, "event": {"type": "thinking", "content": c}}
    for i in range(300):
        await sink.put({"type": "thinking", "content": f"P{i} "})
        for cid in ("a", "b", "c"):
            await sink.put(child(cid, f"{cid}{i} "))
    await sink.put({"type": "tool_call", "tool": "x", "args": {}, "call_id": "t"})
    await sink.put({"type": "done"})
    rows = _rows(db, "r10")
    assert len(rows) <= 12                                     # 1200 deltas → a handful of rows
    parent = "".join(r["content"] for r in rows if r["type"] == "thinking")
    assert parent == "".join(f"P{i} " for i in range(300))
    for cid in ("a", "b", "c"):
        txt = "".join(r["event"]["content"] for r in rows if r["type"] == "subagent_event" and r["parent_call_id"] == cid)
        assert txt == "".join(f"{cid}{i} " for i in range(300))
    assert rows[-2]["type"] == "tool_call" and rows[-1]["type"] == "done"
    assert all(r["type"] != "tool_call" for r in rows[:-2])    # every buffer flushed before it


@pytest.mark.asyncio
async def test_scope_key_change_flushes_only_that_scope(tmp_path):
    db = _db(tmp_path)
    sink = rs.RunSink("r11", "s1", db)
    await sink.put({"type": "thinking", "content": "t"})
    await sink.put({"type": "subagent_event", "parent_call_id": "a", "depth": 1, "event": {"type": "thinking", "content": "x"}})
    await sink.put({"type": "subagent_event", "parent_call_id": "a", "depth": 1, "event": {"type": "message_delta", "content": "m"}})
    assert [r.get("type") for r in _rows(db, "r11")] == ["subagent_event"]    # only child a's thinking flushed
    assert _rows(db, "r11")[0]["event"]["content"] == "x"
    sink.flush()
    assert [(r["type"], r.get("content") or r["event"]["content"]) for r in _rows(db, "r11")] == [
        ("subagent_event", "x"), ("thinking", "t"), ("subagent_event", "m")]


def test_sweep_is_batched(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "SWEEP_BATCH", 7)
    db = _db(tmp_path)
    now = int(time.time())
    db.executemany("INSERT INTO event_log (run_id, seq, payload, created_at) VALUES (?,?,'{}',?)",
                   [(f"r{i}", 1, now - 40 * 86400) for i in range(20)] + [("keep", 1, now)])
    db.commit()
    assert rs.sweep_event_log(db, ttl_days=30, now=now) == 20
    assert db.execute("SELECT COUNT(*) FROM event_log").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_async_sweep_yields_between_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "SWEEP_BATCH", 5)
    db = _db(tmp_path)
    now = int(time.time())
    db.executemany("INSERT INTO event_log (run_id, seq, payload, created_at) VALUES (?,?,'{}',?)",
                   [(f"r{i}", 1, now - 40 * 86400) for i in range(12)])
    db.commit()
    ticks = 0
    async def ticker():
        nonlocal ticks
        for _ in range(50):
            ticks += 1
            await asyncio.sleep(0.01)
    task = asyncio.ensure_future(ticker())
    n = await rs.sweep_event_log_async(db, ttl_days=30, pause=0.02)
    task.cancel()
    assert n == 12 and ticks >= 2                                # other tasks ran meanwhile


def test_ttl_env_override(monkeypatch):
    import importlib
    monkeypatch.setenv("NIMOOS_EVENT_LOG_TTL_DAYS", "7")
    mod = importlib.reload(rs)
    try:
        assert mod.EVENT_LOG_TTL_DAYS == 7
    finally:
        monkeypatch.delenv("NIMOOS_EVENT_LOG_TTL_DAYS")
        importlib.reload(rs)
