"""Tests for WikiContextBuilder."""
from __future__ import annotations

import time

import httpx
import pytest

from wiki_client import WikiClient
from wiki_context import WikiContextBuilder


def _build_client(handler):
    transport = httpx.MockTransport(handler)
    c = WikiClient(user_id="u1", base_url="http://w.test")
    c._http = httpx.AsyncClient(transport=transport, base_url="http://w.test")
    return c


@pytest.mark.asyncio
async def test_build_returns_placeholder_when_wiki_unreachable():
    async def h(req):
        return httpx.Response(503)

    c = _build_client(h)
    out = await WikiContextBuilder(c).build(user_patterns=[])
    assert "Wiki 服务暂不可用" in out
    assert "## NimoOS 存储空间地图" in out


@pytest.mark.asyncio
async def test_build_renders_map_and_notes():
    now_ms = int(time.time() * 1000)
    fresh = now_ms - 5 * 86_400_000
    stale = now_ms - 60 * 86_400_000

    tree = [
        {"path": "/DATA", "level": "space", "ai_label": "主盘",
         "user_notes_updated_at": 0, "last_modified_ms": now_ms},
        {"path": "/DATA/Projects/proj1", "level": "project",
         "ai_label": "项目一", "user_notes_updated_at": fresh,
         "last_modified_ms": now_ms},
        {"path": "/DATA/Projects/proj2", "level": "project",
         "ai_label": "", "user_notes_updated_at": stale,
         "last_modified_ms": now_ms - 1000},
    ]

    async def h(req):
        if req.url.path == "/v1/wiki/tree":
            return httpx.Response(200, json=tree)
        if req.url.path == "/v1/wiki/node":
            return httpx.Response(200, json={
                "path": req.url.params["path"],
                "user_notes": "active notes here",
                "etag": "e1",
            })
        return httpx.Response(404)

    c = _build_client(h)
    out = await WikiContextBuilder(c).build(user_patterns=[])
    assert "## NimoOS 存储空间地图" in out
    assert "/DATA" in out
    assert "项目一" in out
    # proj2 has no ai_label → fallback to basename + "(未生成摘要)"
    assert "proj2" in out
    assert "(未生成摘要)" in out
    assert "## 用户笔记" in out
    # Only proj1 (fresh notes) should appear in the notes section
    assert "/DATA/Projects/proj1" in out
    # proj2 notes are stale → not in notes section (but is in map)
    notes_section = out.split("## 用户笔记")[1]
    assert "/DATA/Projects/proj2" not in notes_section


@pytest.mark.asyncio
async def test_build_caps_projects_to_top_15():
    now_ms = int(time.time() * 1000)
    tree = [{"path": "/DATA", "level": "space", "ai_label": "主盘",
             "user_notes_updated_at": 0, "last_modified_ms": now_ms}]
    for i in range(25):
        tree.append({
            "path": f"/DATA/p{i:02d}", "level": "project",
            "ai_label": f"L{i}",
            "user_notes_updated_at": 0,
            "last_modified_ms": now_ms - i,  # higher i = older
        })

    async def h(req):
        if req.url.path == "/v1/wiki/tree":
            return httpx.Response(200, json=tree)
        return httpx.Response(404)

    c = _build_client(h)
    out = await WikiContextBuilder(c).build(user_patterns=[])
    # Top 15 by last_modified means p00..p14 included, p15..p24 in tail msg
    for i in range(15):
        assert f"/DATA/p{i:02d}" in out
    for i in range(15, 25):
        assert f"/DATA/p{i:02d}" not in out
    assert "还有 10 个项目" in out


@pytest.mark.asyncio
async def test_build_filters_by_user_patterns():
    now_ms = int(time.time() * 1000)
    tree = [
        {"path": "/DATA", "level": "space", "ai_label": "主盘",
         "user_notes_updated_at": 0, "last_modified_ms": now_ms},
        {"path": "/DATA/secret/proj", "level": "project",
         "ai_label": "S", "user_notes_updated_at": 0,
         "last_modified_ms": now_ms},
        {"path": "/DATA/public/proj", "level": "project",
         "ai_label": "P", "user_notes_updated_at": 0,
         "last_modified_ms": now_ms},
    ]

    async def h(req):
        if req.url.path == "/v1/wiki/tree":
            return httpx.Response(200, json=tree)
        return httpx.Response(404)

    c = _build_client(h)
    out = await WikiContextBuilder(c).build(user_patterns=["DATA/secret/**"])
    assert "/DATA/public/proj" in out
    assert "/DATA/secret/proj" not in out


@pytest.mark.asyncio
async def test_build_truncates_long_notes():
    now_ms = int(time.time() * 1000)
    fresh = now_ms - 1 * 86_400_000

    tree = [
        {"path": "/DATA", "level": "space", "ai_label": "主盘",
         "user_notes_updated_at": 0, "last_modified_ms": now_ms},
        {"path": "/DATA/p", "level": "project", "ai_label": "P",
         "user_notes_updated_at": fresh,
         "last_modified_ms": now_ms},
    ]
    long_notes = "Y" * 1000

    async def h(req):
        if req.url.path == "/v1/wiki/tree":
            return httpx.Response(200, json=tree)
        if req.url.path == "/v1/wiki/node":
            return httpx.Response(200, json={
                "path": "/DATA/p", "user_notes": long_notes, "etag": "e1"})
        return httpx.Response(404)

    c = _build_client(h)
    out = await WikiContextBuilder(c).build(user_patterns=[])
    # Per-node cap: 500 chars
    assert "Y" * 500 in out
    assert "Y" * 501 not in out
    assert "更多见 wiki_get_node" in out
