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
    await sink.put(w("p2", "thinking", "X"))          # different child → new row
    await sink.put(w("p1", "thinking", "c"))          # back to p1 → new row (order preserved)
    await sink.put(w("p1", "tool_call", "ignored"))   # non-delta inner → own row
    await sink.put({"type": "done"})
    rows = _rows(db, "r3")
    assert [(r["parent_call_id"], r["event"]["type"], r["event"].get("content")) for r in rows[:-1]] == [
        ("p1", "thinking", "ab"), ("p2", "thinking", "X"), ("p1", "thinking", "c"), ("p1", "tool_call", "ignored")]


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
async def test_timer_flushes_idle_buffer(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "FLUSH_INTERVAL", 0.05)
    db = _db(tmp_path)
    sink = rs.RunSink("r6", "s1", db)
    await sink.put({"type": "thinking", "content": "slow"})
    assert _rows(db, "r6") == []                       # buffered
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
