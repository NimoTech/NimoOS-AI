import asyncio
import json
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from db import init_db
import notes_extract
from notes import store as notes_store


def _conn(tmp_path):
    conn = init_db(str(tmp_path / "m.db"))
    notes_store.set_notes_root(conn, str(tmp_path / "Notes"))
    return conn


async def _fake_indexer_ok(note, body):
    return True


def test_prompt_redacts_fenced_and_lists_existing():
    history = [{"role": "user", "content":
                "<untrusted-data source=\"web\">SECRET-INJECTED</untrusted-data> 结论:买 64G"}]
    p = notes_extract.build_extraction_prompt(history, ["旧笔记标题"])
    assert "SECRET-INJECTED" not in p
    assert "旧笔记标题" in p
    assert "[external-data omitted]" in p


def test_parse_valid_caps_at_three():
    raw = json.dumps({"notes": [
        {"title": f"t{i}", "description": "d", "body": "b", "tags": ["x"]}
        for i in range(5)]})
    out = notes_extract.parse_extraction(raw)
    assert len(out) == 3
    assert out[0] == {"title": "t0", "description": "d", "body": "b", "tags": ["x"]}


def test_parse_rejects_malformed():
    assert notes_extract.parse_extraction("not json") is None
    assert notes_extract.parse_extraction(json.dumps({"notes": "nope"})) is None
    # entries without non-empty title/body are dropped, not fatal
    out = notes_extract.parse_extraction(json.dumps(
        {"notes": [{"title": "", "body": "b"}, {"title": "ok", "body": "b"}]}))
    assert [n["title"] for n in out] == ["ok"]


def test_apply_creates_draft_insight(tmp_path):
    conn = _conn(tmp_path)
    created = asyncio.run(notes_extract.apply_extraction(
        conn, "1", "sess-1",
        [{"title": "内存结论", "description": "d", "body": "买 64G", "tags": ["hw"]}],
        note_indexer=_fake_indexer_ok))
    assert len(created) == 1
    row = conn.execute("SELECT type, status, created_by, source_refs_json "
                       "FROM notes WHERE id=?", (created[0]["id"],)).fetchone()
    assert (row["type"], row["status"], row["created_by"]) == \
        ("insight", "draft", "pipeline")
    assert json.loads(row["source_refs_json"]) == [{"session_id": "sess-1"}]


def test_apply_dedups_by_session_title(tmp_path):
    conn = _conn(tmp_path)
    note = [{"title": "同题", "description": "", "body": "v1", "tags": []}]
    asyncio.run(notes_extract.apply_extraction(
        conn, "1", "sess-1", note, note_indexer=_fake_indexer_ok))
    again = asyncio.run(notes_extract.apply_extraction(
        conn, "1", "sess-1", note, note_indexer=_fake_indexer_ok))
    assert again == []
    n = conn.execute("SELECT COUNT(*) c FROM notes WHERE user_id='1'").fetchone()
    assert n["c"] == 1


def test_apply_index_failure_sets_sentinel(tmp_path):
    conn = _conn(tmp_path)

    async def bad_indexer(note, body):
        return False

    created = asyncio.run(notes_extract.apply_extraction(
        conn, "1", "sess-1",
        [{"title": "哨兵", "description": "", "body": "b", "tags": []}],
        note_indexer=bad_indexer))
    row = conn.execute("SELECT content_hash FROM notes WHERE id=?",
                       (created[0]["id"],)).fetchone()
    assert row["content_hash"] == ""


def test_worker_end_to_end_creates_draft(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO sessions (id, user_id, source, created_at, updated_at) "
                 "VALUES ('s1','1','web',0,0)")
    conn.commit()
    notes_extract.maybe_enqueue_notes_job(
        conn, "s1", "1", now=100, provider_url="u", provider_key="k",
        provider_type="openai", model_name="m")

    async def llm(job, prompt):
        assert "Conversation" in prompt
        return json.dumps({"notes": [{"title": "结论", "description": "d",
                                      "body": "买 64G", "tags": []}]})

    ran = asyncio.run(notes_extract.process_pending_once(
        conn, llm_call=llm, history_loader=lambda sid: [{"role": "user", "content": "hi"}],
        note_indexer=_fake_indexer_ok, now=100 + notes_extract.IDLE_SECONDS + 1))
    assert ran is True
    row = conn.execute("SELECT status, type FROM notes WHERE user_id='1'").fetchone()
    assert (row["status"], row["type"]) == ("draft", "insight")
    assert conn.execute("SELECT COUNT(*) c FROM notes_extract_jobs").fetchone()["c"] == 0


def test_worker_respects_idle_gate(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO sessions (id, user_id, source, created_at, updated_at) "
                 "VALUES ('s1','1','web',0,0)")
    conn.commit()
    notes_extract.maybe_enqueue_notes_job(
        conn, "s1", "1", now=100, provider_url="u", provider_key="k",
        provider_type="openai", model_name="m")
    ran = asyncio.run(notes_extract.process_pending_once(
        conn, llm_call=None, history_loader=None, now=100 + 30))
    assert ran is False  # still idle-gated; llm_call must not be touched


def test_worker_retries_then_gives_up(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO sessions (id, user_id, source, created_at, updated_at) "
                 "VALUES ('s1','1','web',0,0)")
    conn.commit()
    notes_extract.maybe_enqueue_notes_job(
        conn, "s1", "1", now=0, provider_url="u", provider_key="k",
        provider_type="openai", model_name="m")

    async def boom(job, prompt):
        raise RuntimeError("llm down")

    for attempt in range(notes_extract.MAX_ATTEMPTS):
        asyncio.run(notes_extract.process_pending_once(
            conn, llm_call=boom, history_loader=lambda sid: [], now=1000))
    assert conn.execute("SELECT COUNT(*) c FROM notes_extract_jobs").fetchone()["c"] == 0
