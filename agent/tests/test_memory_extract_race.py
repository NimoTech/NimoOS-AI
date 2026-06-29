import pytest
import memory_extract as mx


@pytest.fixture
def conn(tmp_path):
    from db import init_db
    c = init_db(str(tmp_path / "m.db"))
    yield c
    c.close()


def _seed(conn, sid="s1", user="u1", enq=1000):
    mx.maybe_enqueue_extract_job(conn, sid, user, provider_url="u",
        provider_key="k", provider_type="t", model_name="m", now=enq)


@pytest.mark.asyncio
async def test_success_delete_preserves_reenqueue(conn):
    _seed(conn)
    def hist(_s): return [{"role": "user", "content": "I'm an engineer"}]
    async def llm(job, prompt):
        # user sends another message mid-extraction → re-enqueue (status → pending)
        mx.maybe_enqueue_extract_job(conn, "s1", "u1", provider_url="u",
            provider_key="k", provider_type="t", model_name="m", now=2500)
        return '{"actions":[],"referenced":[]}'
    await mx.process_pending_once(conn, llm_call=llm, history_loader=hist, now=2000)
    row = conn.execute("SELECT status FROM memory_extract_jobs WHERE session_id='s1'"
                       ).fetchone()
    assert row is not None and row["status"] == "pending"   # not clobbered
