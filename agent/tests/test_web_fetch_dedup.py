import pytest

import tool_output as to
from skills import web as web_skill
from skills.search import search as search_skill


def test_dedup_key_drops_fragment_and_sorts_query():
    a = web_skill.dedup_key("https://Example.com/a?b=2&a=1#frag")
    b = web_skill.dedup_key("https://example.com/a?a=1&b=2")
    assert a == b
    assert web_skill.dedup_key("not a url") == "not a url"


@pytest.mark.asyncio
async def test_second_fetch_of_same_url_in_run_is_skipped(monkeypatch):
    calls = []

    async def fake_fetch_page(url, *, max_chars=30000, client=None):
        calls.append(url)
        return {"url": url, "final_url": url, "text": "body", "truncated": False}

    monkeypatch.setattr(web_skill._fetch, "fetch_page", fake_fetch_page)
    monkeypatch.setattr(web_skill, "audit", lambda *a, **k: None)
    to.RUN_SCRATCH_VAR.set({})
    to.CALL_ID_VAR.set("call_first")
    first = await web_skill._web_fetch_impl("https://example.com/p?x=1#top", 30000)
    to.CALL_ID_VAR.set("call_second")
    second = await web_skill._web_fetch_impl("https://example.com/p?x=1", 30000)
    assert len(calls) == 1
    assert "body" in first
    assert "call_first" in second and "already fetched" in second


@pytest.mark.asyncio
async def test_failed_fetch_is_not_remembered(monkeypatch):
    n = {"i": 0}

    async def fake_fetch_page(url, *, max_chars=30000, client=None):
        n["i"] += 1
        if n["i"] == 1:
            return {"error": "HTTP 429 from " + url}
        return {"url": url, "final_url": url, "text": "ok", "truncated": False}

    monkeypatch.setattr(web_skill._fetch, "fetch_page", fake_fetch_page)
    monkeypatch.setattr(web_skill, "audit", lambda *a, **k: None)
    to.RUN_SCRATCH_VAR.set({})
    await web_skill._web_fetch_impl("https://example.com/r", 30000)
    out = await web_skill._web_fetch_impl("https://example.com/r", 30000)
    assert n["i"] == 2 and "ok" in out


@pytest.mark.asyncio
async def test_different_max_chars_is_a_different_key(monkeypatch):
    calls = []

    async def fake_fetch_page(url, *, max_chars=30000, client=None):
        calls.append(max_chars)
        return {"url": url, "final_url": url, "text": "t", "truncated": True}

    monkeypatch.setattr(web_skill._fetch, "fetch_page", fake_fetch_page)
    monkeypatch.setattr(web_skill, "audit", lambda *a, **k: None)
    to.RUN_SCRATCH_VAR.set({})
    await web_skill._web_fetch_impl("https://example.com/q", 10000)
    await web_skill._web_fetch_impl("https://example.com/q", 60000)
    assert calls == [10000, 60000]


@pytest.mark.asyncio
async def test_no_scratch_var_means_no_dedup(monkeypatch):
    calls = []

    async def fake_fetch_page(url, *, max_chars=30000, client=None):
        calls.append(url)
        return {"url": url, "final_url": url, "text": "t", "truncated": False}

    monkeypatch.setattr(web_skill._fetch, "fetch_page", fake_fetch_page)
    monkeypatch.setattr(web_skill, "audit", lambda *a, **k: None)
    # simulate a caller that never set RUN_SCRATCH_VAR (fresh context)
    import contextvars
    ctx = contextvars.Context()

    async def run():
        await web_skill._web_fetch_impl("https://example.com/z", 30000)
        await web_skill._web_fetch_impl("https://example.com/z", 30000)

    import asyncio
    await asyncio.get_running_loop().create_task(run(), context=ctx)
    assert len(calls) == 2


def test_read_document_clamps_max_chars():
    # The impl fans out to Search/Parser over HTTP; pin the clamp by source so
    # the test needs no network stubs.
    import inspect
    src = inspect.getsource(search_skill._read_document_impl)
    assert "max_chars = max(500, min(int(max_chars or 24000), 60000))" in src
