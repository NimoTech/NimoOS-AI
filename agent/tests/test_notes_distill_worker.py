import asyncio
import json
import os
import sys, pathlib
import types
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import httpx
import pytest

from db import init_db
import notes_distill
from notes import store as notes_store

PARSED_JSON = json.dumps({"title": "T", "description": "d",
                          "body": "B", "tags": []})


class _OSProxy:
    """Delegates everything except `stat` to the real `os` module. Used to
    fake `notes_distill.os.stat` without mutating the process-wide `os`
    module in place: an earlier version of this fixture did
    `monkeypatch.setattr(notes_distill.os, "stat", ...)` (patching the real,
    shared module object) and that left pytest's own tmp_path/cache
    bookkeeping calling the stub after this test's teardown, crashing the
    whole session — rebinding the module-level name `notes_distill.os` to a
    throwaway proxy object instead keeps the blast radius to this module."""

    def __init__(self, stat_fn):
        self.stat = stat_fn

    def __getattr__(self, name):
        return getattr(os, name)


@pytest.fixture(autouse=True)
def _fake_stat_for_conventional_data_paths(monkeypatch):
    """Most of this file's tests seed jobs under the fictitious '/DATA/...'
    convention this suite uses for "pretend file" — never real files on
    disk, which was harmless before the size-cap check added a mandatory
    os.stat() call to process_pending_once. Fake a small, in-cap size only
    for that prefix; any other path (e.g. a real tmp_path file, or a
    deliberately-missing one) falls through to the real os.stat unchanged,
    which is exactly what the size-cap and vanished-file tests below need."""
    def _stat(path, *a, **kw):
        if str(path).startswith("/DATA/"):
            return types.SimpleNamespace(st_size=1024)
        return os.stat(path, *a, **kw)

    monkeypatch.setattr(notes_distill, "os", _OSProxy(_stat))


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
    # Tombstoned as 'skipped', not deleted — a DELETE would drop the file out
    # of notes_distill_scan._known_mtimes and cause an infinite
    # re-enqueue/re-attempt loop on every scan pass (C3).
    row = conn.execute("SELECT status FROM notes_distill_jobs").fetchone()
    assert row is not None and row["status"] == "skipped"


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
    # A non-retryable 4xx forces attempts straight to MAX_ATTEMPTS, so
    # fail_job tombstones as 'failed' rather than deleting (C3) — the row
    # stays in notes_distill_scan._known_mtimes so an unchanged file is
    # never re-enqueued.
    row = conn.execute("SELECT status FROM notes_distill_jobs").fetchone()
    assert row is not None and row["status"] == "failed"


def test_creds_unresolved_is_retried_then_tombstones_as_failed(tmp_path):
    """Post-final-review Minor #1: credential resolution failure is
    recoverable (Go internal-token endpoint restart window, token file
    momentarily unreadable), unlike the other two drop paths — so it must go
    through fail_job (retryable) rather than skip_job (terminal on the very
    first hiccup)."""
    conn = _conn(tmp_path)
    _seed(conn)

    async def creds_none(user_id, model):
        return None

    async def llm(creds, prompt):
        raise AssertionError("LLM must not be called")

    asyncio.run(notes_distill.process_pending_once(
        conn, llm_call=llm, extractor=_extract_short, creds_resolver=creds_none,
        note_indexer=_ok, now=100, day="20260727"))
    row = conn.execute("SELECT status, attempts FROM notes_distill_jobs"
                       ).fetchone()
    assert row["status"] == "pending" and row["attempts"] == 1

    # Exhaust the remaining attempts; at MAX_ATTEMPTS the row finally
    # tombstones as 'failed' (not deleted, not 'skipped').
    for i in range(notes_distill.MAX_ATTEMPTS - 1):
        asyncio.run(notes_distill.process_pending_once(
            conn, llm_call=llm, extractor=_extract_short,
            creds_resolver=creds_none, note_indexer=_ok, now=100 + i,
            day="20260727"))
    row = conn.execute("SELECT status, attempts FROM notes_distill_jobs"
                       ).fetchone()
    assert row["status"] == "failed"
    assert row["attempts"] == notes_distill.MAX_ATTEMPTS


def test_empty_extract_text_drops_job_as_skipped(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn)

    async def extract_empty(path, max_chars):
        return {"markdown": "   ", "truncated": False}

    async def llm(creds, prompt):
        raise AssertionError("LLM must not be called")

    asyncio.run(notes_distill.process_pending_once(
        conn, llm_call=llm, extractor=extract_empty, creds_resolver=_creds,
        note_indexer=_ok, now=100, day="20260727"))
    row = conn.execute("SELECT status FROM notes_distill_jobs").fetchone()
    assert row is not None and row["status"] == "skipped"


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


def test_empty_str_error_falls_back_to_exception_type_name(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn)

    async def extract_timeout(path, max_chars):
        raise asyncio.TimeoutError()

    async def llm(creds, prompt):
        return PARSED_JSON

    asyncio.run(notes_distill.process_pending_once(
        conn, llm_call=llm, extractor=extract_timeout, creds_resolver=_creds,
        note_indexer=_ok, now=100, day="20260727"))
    row = conn.execute("SELECT status, last_error FROM notes_distill_jobs"
                       ).fetchone()
    assert row["status"] == "pending"
    assert row["last_error"] == "TimeoutError"


def test_error_message_redacts_api_key(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn)

    async def creds_with_secret(user_id, model):
        return {"provider_type": "other", "base_url": "http://x/v1",
                "api_key": "sk-supersecret123", "model": "m"}

    async def extract_500_leaky(path, max_chars):
        raise httpx.HTTPStatusError(
            "500 server error, request echoed key sk-supersecret123",
            request=httpx.Request("POST", "http://x"),
            response=httpx.Response(500))

    async def llm(creds, prompt):
        return PARSED_JSON

    asyncio.run(notes_distill.process_pending_once(
        conn, llm_call=llm, extractor=extract_500_leaky,
        creds_resolver=creds_with_secret, note_indexer=_ok, now=100,
        day="20260727"))
    row = conn.execute("SELECT status, last_error FROM notes_distill_jobs"
                       ).fetchone()
    assert row["status"] == "pending"
    assert "sk-supersecret123" not in row["last_error"]
    assert "***" in row["last_error"]


def test_oversized_file_is_skipped_without_extract_or_llm(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _seed(conn, path="/DATA/big.pdf")

    class _FakeStat:
        st_size = notes_distill.MAX_DISTILL_BYTES + 1

    monkeypatch.setattr(notes_distill.os, "stat", lambda path: _FakeStat())

    async def extractor(path, max_chars):
        raise AssertionError("extractor must not be called")

    async def llm(creds, prompt):
        raise AssertionError("LLM must not be called")

    asyncio.run(notes_distill.process_pending_once(
        conn, llm_call=llm, extractor=extractor, creds_resolver=_creds,
        note_indexer=_ok, now=100, day="20260727"))
    row = conn.execute("SELECT status, last_error FROM notes_distill_jobs"
                       ).fetchone()
    assert row is not None
    assert row["status"] == "skipped"
    assert row["last_error"] == "file too large for distillation"


def test_file_at_exactly_the_size_cap_proceeds_to_extract(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _seed(conn, path="/DATA/exactly-cap.pdf")

    class _FakeStat:
        st_size = notes_distill.MAX_DISTILL_BYTES

    monkeypatch.setattr(notes_distill.os, "stat", lambda path: _FakeStat())
    calls = []

    async def llm(creds, prompt):
        calls.append(prompt)
        return PARSED_JSON

    assert asyncio.run(notes_distill.process_pending_once(
        conn, llm_call=llm, extractor=_extract_short, creds_resolver=_creds,
        note_indexer=_ok, now=100, day="20260727"))
    assert len(calls) == 1
    row = conn.execute("SELECT COUNT(*) c FROM notes_distill_jobs").fetchone()
    assert row["c"] == 0   # finish_job deleted it — the happy path completed


def test_vanished_file_is_tombstoned_not_deleted(tmp_path):
    """Post-review fix: a stat failure (missing file, or a transient
    ESTALE/EIO blip on a flaky network mount where the file still exists)
    must NOT silently DELETE the row via finish_job — that would erase it
    from notes_distill_scan._known_mtimes with no diagnostic trail, and on
    a flaky mount the very next scan pass would re-enqueue and hit the same
    blip again: perpetual churn, zero visibility. It must tombstone via
    skip_job instead, same as the other terminal drop paths in this
    function, so the reason is visible in the queue page and there is
    exactly one row, not a retry loop."""
    conn = _conn(tmp_path)
    missing_path = str(tmp_path / "does-not-exist.pdf")
    _seed(conn, path=missing_path)

    async def extractor(path, max_chars):
        raise AssertionError("extractor must not be called")

    async def llm(creds, prompt):
        raise AssertionError("LLM must not be called")

    assert asyncio.run(notes_distill.process_pending_once(
        conn, llm_call=llm, extractor=extractor, creds_resolver=_creds,
        note_indexer=_ok, now=100, day="20260727"))
    row = conn.execute("SELECT status, last_error FROM notes_distill_jobs"
                       ).fetchone()
    assert row is not None
    assert row["status"] == "skipped"
    assert row["last_error"] == "file missing or unreachable"


def test_post_claim_quota_recheck_reverts_a_different_users_overrun_job(
        tmp_path, monkeypatch):
    """I1: process_pending_once's quota probe only inspects the
    globally-oldest pending row's user; claim_job's own ORDER BY can in
    principle hand back a DIFFERENT user's row. Simulate that mismatch
    directly (rather than fighting claim_job's real ordering/ties) by
    stubbing claim_job to claim a specific auto job belonging to a user whose
    quota is already exhausted, and assert process_pending_once notices post
    -claim, returns the row to pending with attempts restored, and never
    calls the LLM."""
    conn = _conn(tmp_path)
    notes_store.set_background_model(conn, "u2", "cloud:1:m")
    # A pending row so the top-of-function probe has something to examine
    # (any user — its quota state is irrelevant to this test).
    notes_distill.enqueue(conn, file_path="/DATA/probe.pdf", user_id="u1",
                          root_id="r1", file_mtime=1, now=1)
    # The row that will actually be "claimed" (via the stub below), owned by
    # a different user who has already spent today's quota.
    notes_distill.enqueue(conn, file_path="/DATA/a.pdf", user_id="u2",
                          root_id="r1", file_mtime=1, now=2)
    day = "20260727"
    for _ in range(notes_store.get_daily_cap(conn, "u2")):
        notes_store.quota_consume(conn, "u2", day=day)
    assert notes_store.quota_remaining(conn, "u2", day=day) == 0

    def _fake_claim(conn, *, quota_ok, now=None):
        conn.execute(
            "UPDATE notes_distill_jobs SET status='running', "
            "attempts=attempts+1, updated_at=? WHERE file_path=?",
            (now or 0, "/DATA/a.pdf"))
        conn.commit()
        return conn.execute(
            "SELECT * FROM notes_distill_jobs WHERE file_path=?",
            ("/DATA/a.pdf",)).fetchone()

    monkeypatch.setattr(notes_distill, "claim_job", _fake_claim)

    async def llm(creds, prompt):
        raise AssertionError("LLM must not be called")

    async def extractor(path, max_chars):
        raise AssertionError("extractor must not be called")

    async def creds_resolver(user_id, model):
        raise AssertionError("creds resolver must not be called")

    done = asyncio.run(notes_distill.process_pending_once(
        conn, llm_call=llm, extractor=extractor, creds_resolver=creds_resolver,
        note_indexer=_ok, now=100, day=day))
    assert done is False
    row = conn.execute("SELECT status, attempts FROM notes_distill_jobs "
                       "WHERE file_path=?", ("/DATA/a.pdf",)).fetchone()
    assert row["status"] == "pending"
    assert row["attempts"] == 0   # restored to its pre-claim value
