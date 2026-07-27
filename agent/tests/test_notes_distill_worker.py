import asyncio
import json
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import httpx

from db import init_db
import notes_distill
from notes import store as notes_store

PARSED_JSON = json.dumps({"title": "T", "description": "d",
                          "body": "B", "tags": []})


def _conn(tmp_path):
    conn = init_db(str(tmp_path / "m.db"))
    notes_store.set_notes_root(conn, str(tmp_path / "Notes"))
    notes_store.set_background_model(conn, "u1", "cloud:1:m")
    return conn


async def _creds(user_id, model):
    return {"provider_type": "other", "base_url": "http://x/v1",
            "api_key": "k", "model": "m"}


async def _extract_short(path, max_chars):
    return {"markdown": "short text", "truncated": False}


async def _ok(note, body):
    return True


def _seed(conn, path="/DATA/a.pdf"):
    notes_distill.enqueue(conn, file_path=path, user_id="u1",
                          root_id="r1", file_mtime=1, now=10)


def test_no_job_returns_false(tmp_path):
    conn = _conn(tmp_path)
    done = asyncio.run(notes_distill.process_pending_once(
        conn, llm_call=None, extractor=_extract_short,
        creds_resolver=_creds, note_indexer=_ok, now=100, day="20260727"))
    assert done is False


def test_happy_path_creates_note_and_consumes_quota(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn)
    calls = []

    async def llm(creds, prompt):
        calls.append(prompt)
        return PARSED_JSON

    assert asyncio.run(notes_distill.process_pending_once(
        conn, llm_call=llm, extractor=_extract_short, creds_resolver=_creds,
        note_indexer=_ok, now=100, day="20260727"))
    assert len(calls) == 1
    assert conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM notes_distill_jobs"
                        ).fetchone()["c"] == 0
    assert notes_store.quota_remaining(conn, "u1", day="20260727") == 49


def test_manual_job_does_not_consume_quota(tmp_path):
    conn = _conn(tmp_path)
    notes_distill.enqueue(conn, file_path="/DATA/a.pdf", user_id="u1",
                          root_id="r1", file_mtime=1, origin="manual", now=10)

    async def llm(creds, prompt):
        return PARSED_JSON

    asyncio.run(notes_distill.process_pending_once(
        conn, llm_call=llm, extractor=_extract_short, creds_resolver=_creds,
        note_indexer=_ok, now=100, day="20260727"))
    assert notes_store.quota_remaining(conn, "u1", day="20260727") == 50


def test_unconfigured_background_model_drops_job_without_llm(tmp_path):
    conn = _conn(tmp_path)
    notes_store.set_background_model(conn, "u1", "")
    _seed(conn)

    async def llm(creds, prompt):
        raise AssertionError("LLM must not be called")

    asyncio.run(notes_distill.process_pending_once(
        conn, llm_call=llm, extractor=_extract_short, creds_resolver=_creds,
        note_indexer=_ok, now=100, day="20260727"))
    assert conn.execute("SELECT COUNT(*) c FROM notes_distill_jobs"
                        ).fetchone()["c"] == 0


def test_long_document_takes_map_reduce_path(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn)
    prompts = []

    async def extract_long(path, max_chars):
        return {"markdown": "y" * (notes_distill.CHUNK_CHARS * 3),
                "truncated": True}

    async def llm(creds, prompt):
        prompts.append(prompt)
        return PARSED_JSON

    asyncio.run(notes_distill.process_pending_once(
        conn, llm_call=llm, extractor=extract_long, creds_resolver=_creds,
        note_indexer=_ok, now=100, day="20260727"))
    assert len(prompts) == 4          # 3 map + 1 reduce
    row = conn.execute("SELECT source_refs_json FROM notes").fetchone()
    assert '"truncated": true' in row["source_refs_json"]


def test_parser_4xx_is_not_retried(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn)

    async def extract_403(path, max_chars):
        raise httpx.HTTPStatusError(
            "403", request=httpx.Request("POST", "http://x"),
            response=httpx.Response(403))

    async def llm(creds, prompt):
        raise AssertionError("LLM must not be called")

    asyncio.run(notes_distill.process_pending_once(
        conn, llm_call=llm, extractor=extract_403, creds_resolver=_creds,
        note_indexer=_ok, now=100, day="20260727"))
    assert conn.execute("SELECT COUNT(*) c FROM notes_distill_jobs"
                        ).fetchone()["c"] == 0


def test_parser_5xx_is_retried(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn)

    async def extract_500(path, max_chars):
        raise httpx.HTTPStatusError(
            "500", request=httpx.Request("POST", "http://x"),
            response=httpx.Response(500))

    async def llm(creds, prompt):
        return PARSED_JSON

    asyncio.run(notes_distill.process_pending_once(
        conn, llm_call=llm, extractor=extract_500, creds_resolver=_creds,
        note_indexer=_ok, now=100, day="20260727"))
    row = conn.execute("SELECT status, attempts FROM notes_distill_jobs"
                       ).fetchone()
    assert row["status"] == "pending" and row["attempts"] == 1


def test_pace_seconds_zero_when_idle_and_grows_under_load():
    assert notes_distill.pace_seconds(0.1) == 0.0
    assert notes_distill.pace_seconds(0.7) == 0.0
    assert notes_distill.pace_seconds(1.4) > 0.0
    assert notes_distill.pace_seconds(99.0) <= notes_distill.PACE_MAX
