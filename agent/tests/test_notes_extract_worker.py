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
