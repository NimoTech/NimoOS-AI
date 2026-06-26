import asyncio
import pytest
from db import init_db
import memory_store as ms
import memory_extract as mx


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "m.db"))
    yield c
    c.close()


def _seed(conn, sid, user, enq):
    mx.maybe_enqueue_extract_job(conn, sid, user, provider_url="u",
        provider_key="k", provider_type="t", model_name="m", now=enq)


def _hist(_sid):
    return [{"role": "user", "content": "I'm a software engineer"}]


@pytest.mark.asyncio
async def test_idle_gating_skips_fresh_jobs(conn):
    _seed(conn, "s1", "u1", enq=1000)
    # now only 10s later → not idle (IDLE_SECONDS=120) → nothing claimed
    async def llm(job, prompt): return '{"actions":[],"referenced":[]}'
    res = await mx.process_pending_once(conn, llm_call=llm, history_loader=_hist, now=1010)
    assert res is None
    assert conn.execute("SELECT status FROM memory_extract_jobs WHERE session_id='s1'"
                        ).fetchone()["status"] == "pending"


@pytest.mark.asyncio
async def test_success_applies_and_deletes_row(conn):
    _seed(conn, "s1", "u1", enq=1000)
    async def llm(job, prompt):
        return '{"actions":[{"op":"ADD","kind":"fact","text":"is a software engineer"}],"referenced":[]}'
    # now well past idle
    res = await mx.process_pending_once(conn, llm_call=llm, history_loader=_hist, now=2000)
    assert res == "s1"
    assert {r["text"] for r in ms.list_active(conn, "u1")} == {"is a software engineer"}
    assert conn.execute("SELECT COUNT(*) c FROM memory_extract_jobs").fetchone()["c"] == 0


@pytest.mark.asyncio
async def test_timeout_retries_then_errors_and_deletes(conn):
    _seed(conn, "s1", "u1", enq=1000)
    async def llm(job, prompt):
        raise asyncio.TimeoutError()
    # attempt 1 & 2 -> kept pending; attempt 3 -> terminal, row deleted
    for i in range(mx.MAX_ATTEMPTS - 1):
        await mx.process_pending_once(conn, llm_call=llm, history_loader=_hist, now=2000 + i)
        row = conn.execute("SELECT status, attempts FROM memory_extract_jobs WHERE session_id='s1'").fetchone()
        assert row["status"] == "pending"
    await mx.process_pending_once(conn, llm_call=llm, history_loader=_hist, now=3000)
    assert conn.execute("SELECT COUNT(*) c FROM memory_extract_jobs").fetchone()["c"] == 0


@pytest.mark.asyncio
async def test_invalid_json_counts_as_attempt(conn):
    _seed(conn, "s1", "u1", enq=1000)
    async def llm(job, prompt): return "not json"
    await mx.process_pending_once(conn, llm_call=llm, history_loader=_hist, now=2000)
    row = conn.execute("SELECT status, attempts FROM memory_extract_jobs WHERE session_id='s1'").fetchone()
    assert row["attempts"] == 1 and row["status"] == "pending"
