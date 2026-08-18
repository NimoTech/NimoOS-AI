"""skills/web.py — the two tools, their fences, and their registration."""
from __future__ import annotations

import json

import pytest

import db as db_module
from tests.conftest import unfence
from skills import web as web_skill
from web import backends


@pytest.mark.asyncio
async def test_search_returns_fenced_json_with_hits(monkeypatch):
    conn = db_module.init_db(":memory:")
    from web import settings as web_settings
    web_settings.save(conn, backend="tavily", api_key="k",
                      base_url="", enabled=True)
    monkeypatch.setattr(web_skill, "_conn", lambda: conn)

    async def fake_search(self, client, query, *, max_results, days, domains):
        return backends.SearchResult(
            hits=[backends.SearchHit(title="T", url="https://a.test/1",
                                     snippet="s")],
            applied={"days": False, "domains": False})

    monkeypatch.setattr(backends.TavilyBackend, "search", fake_search)

    out = await web_skill._web_search_impl("q", 5, None, None)
    doc = json.loads(unfence(out, source="web-search-results"))
    assert doc["results"][0]["url"] == "https://a.test/1"
    assert doc["applied"] == {"days": False, "domains": False}


@pytest.mark.asyncio
async def test_search_without_backend_says_so(monkeypatch):
    conn = db_module.init_db(":memory:")
    monkeypatch.setattr(web_skill, "_conn", lambda: conn)
    out = await web_skill._web_search_impl("q", 5, None, None)
    assert "not configured" in out


@pytest.mark.asyncio
async def test_search_backend_error_is_returned_as_error(monkeypatch):
    conn = db_module.init_db(":memory:")
    from web import settings as web_settings
    web_settings.save(conn, backend="tavily", api_key="k",
                      base_url="", enabled=True)
    monkeypatch.setattr(web_skill, "_conn", lambda: conn)

    async def failing(self, client, query, *, max_results, days, domains):
        return backends.SearchResult(error="tavily: HTTP 401")

    monkeypatch.setattr(backends.TavilyBackend, "search", failing)
    out = await web_skill._web_search_impl("q", 5, None, None)
    assert "tavily: HTTP 401" in out


@pytest.mark.asyncio
async def test_search_parses_comma_separated_domains(monkeypatch):
    conn = db_module.init_db(":memory:")
    from web import settings as web_settings
    web_settings.save(conn, backend="tavily", api_key="k",
                      base_url="", enabled=True)
    monkeypatch.setattr(web_skill, "_conn", lambda: conn)
    seen = {}

    async def capture(self, client, query, *, max_results, days, domains):
        seen["domains"] = domains
        return backends.SearchResult(hits=[], applied={})

    monkeypatch.setattr(backends.TavilyBackend, "search", capture)
    await web_skill._web_search_impl("q", 5, None, " a.test , b.test ")
    assert seen["domains"] == ["a.test", "b.test"]


@pytest.mark.asyncio
async def test_fetch_returns_fenced_page(monkeypatch):
    async def fake_fetch_page(url, *, max_chars=30000, client=None):
        return {"url": url, "final_url": url, "title": "T",
                "content_markdown": "body text", "truncated": False}

    monkeypatch.setattr(web_skill._fetch, "fetch_page", fake_fetch_page)
    out = await web_skill._web_fetch_impl("https://x.test/a", 30000)
    doc = json.loads(unfence(out, source="web-page"))
    assert doc["title"] == "T"
    assert doc["content_markdown"] == "body text"


@pytest.mark.asyncio
async def test_fetch_redirect_is_surfaced_unfenced(monkeypatch):
    async def fake_fetch_page(url, *, max_chars=30000, client=None):
        return {"redirect_to": "https://other.test/b"}

    monkeypatch.setattr(web_skill._fetch, "fetch_page", fake_fetch_page)
    out = await web_skill._web_fetch_impl("https://x.test/a", 30000)
    assert "https://other.test/b" in out
    assert "call web_fetch again" in out


@pytest.mark.asyncio
async def test_both_tools_write_an_audit_event(monkeypatch, tmp_path):
    import audit as audit_mod
    audit_mod.set_audit_path_for_test(str(tmp_path / "audit.log"))

    conn = db_module.init_db(":memory:")
    monkeypatch.setattr(web_skill, "_conn", lambda: conn)

    async def fake_fetch_page(url, *, max_chars=30000, client=None):
        return {"url": url, "final_url": url, "title": "T",
                "content_markdown": "b", "truncated": False}

    monkeypatch.setattr(web_skill._fetch, "fetch_page", fake_fetch_page)

    await web_skill._web_search_impl("q", 5, None, None)      # unconfigured path
    await web_skill._web_fetch_impl("https://x.test/a", 30000)

    lines = (tmp_path / "audit.log").read_text().splitlines()
    events = [json.loads(x)["event"] for x in lines]
    assert "web_search" in events
    assert "web_fetch" in events


def test_web_category_is_registered():
    from skills import tool_registry as reg
    assert reg.category_of("web_search") == "web"
    assert reg.category_of("web_fetch") == "web"
    assert "web" in reg.CATEGORY_DESCRIPTIONS
    assert "web_search" not in reg.CORE_TOOL_NAMES


def test_web_search_is_skipped_when_unconfigured(monkeypatch):
    import agent as agent_mod
    conn = db_module.init_db(":memory:")
    monkeypatch.setattr(agent_mod, "_web_search_available",
                        lambda: False, raising=False)
    names = [getattr(t, "name", getattr(t, "__name__", ""))
             for t in agent_mod.select_tools_for_run([], session_id="s-none")]
    assert "web_fetch" in names
    assert "web_search" not in names
