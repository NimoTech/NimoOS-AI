"""Tests for agent.skills.wiki read tools + _gate."""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from skills import wiki as wiki_skills
from wiki_client import WikiClient
import httpx
from confirm import ConfirmManager


class _RecorderQueue:
    """Stand-in for the real sink: record events for assertion, no IO."""
    def __init__(self):
        self.events = []

    async def put(self, event):
        self.events.append(event)


@pytest.fixture
def confirm_setup():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE pending_confirmations (
            confirm_id TEXT PRIMARY KEY,
            session_id TEXT,
            action TEXT,
            description TEXT,
            command TEXT,
            created_at INTEGER
        )
    """)
    conn.commit()
    mgr = ConfirmManager(conn, timeout=2.0)
    queue = _RecorderQueue()
    wiki_skills.CONFIRM_MGR_VAR.set(mgr)
    wiki_skills.EVENT_QUEUE_VAR.set(queue)
    wiki_skills.SESSION_ID_VAR.set("s1")
    return mgr, queue


def _make_client(handler) -> WikiClient:
    transport = httpx.MockTransport(handler)
    c = WikiClient("http://wiki.test", user_id="u1")
    c._http = httpx.AsyncClient(transport=transport, base_url="http://wiki.test")
    return c


@pytest.fixture(autouse=True)
def _reset_context():
    """Each test sets ContextVars fresh; pytest fixture resets defaults."""
    wiki_skills.WIKI_CLIENT_VAR.set(None)
    wiki_skills.USER_PATTERNS_VAR.set([])
    yield


@pytest.mark.asyncio
async def test_get_node_returns_error_when_client_none():
    wiki_skills.WIKI_CLIENT_VAR.set(None)
    out = await wiki_skills._wiki_get_node_impl("/a")
    payload = json.loads(out)
    assert payload["error"] == "wiki service unavailable"


@pytest.mark.asyncio
async def test_get_node_returns_node():
    async def h(req):
        return httpx.Response(200, json={"path": "/a", "ai_label": "L"})

    wiki_skills.WIKI_CLIENT_VAR.set(_make_client(h))
    out = await wiki_skills._wiki_get_node_impl("/a")
    payload = json.loads(out)
    assert payload["path"] == "/a"


@pytest.mark.asyncio
async def test_get_node_returns_error_on_404():
    async def h(req):
        return httpx.Response(404)

    wiki_skills.WIKI_CLIENT_VAR.set(_make_client(h))
    out = await wiki_skills._wiki_get_node_impl("/missing")
    payload = json.loads(out)
    assert payload["error"] == "node not found"


@pytest.mark.asyncio
async def test_gate_blocks_blacklisted_path():
    async def h(req):
        pytest.fail("client must not be called")

    wiki_skills.WIKI_CLIENT_VAR.set(_make_client(h))
    wiki_skills.USER_PATTERNS_VAR.set(["secret/**"])
    out = await wiki_skills._wiki_get_node_impl("/secret/file")
    payload = json.loads(out)
    assert payload["error"] == "path is on user's hard blacklist"


@pytest.mark.asyncio
async def test_list_full_tree_passthrough():
    async def h(req):
        return httpx.Response(200, json=[{"path": "/a"}])

    wiki_skills.WIKI_CLIENT_VAR.set(_make_client(h))
    out = await wiki_skills._wiki_list_full_tree_impl("")
    payload = json.loads(out)
    assert payload == [{"path": "/a"}]


@pytest.mark.asyncio
async def test_recent_changes_resolves_root_and_since():
    async def h(req):
        if req.url.path == "/v1/wiki/tree":
            return httpx.Response(200, json=[
                {"path": "/A", "level": "space"},
                {"path": "/A/proj", "level": "project"},
            ])
        if req.url.path == "/v1/wiki/recent-changes":
            params = dict(req.url.params)
            assert params["root_id"] == "/A"  # nearest space ancestor of /A/proj
            assert int(params["since_ms"]) > 0
            assert params["limit"] == "10"
            return httpx.Response(200, json=[{"path": "/A/proj/file", "op": "create"}])
        return httpx.Response(500)

    wiki_skills.WIKI_CLIENT_VAR.set(_make_client(h))
    out = await wiki_skills._wiki_recent_changes_impl("/A/proj", since_days=7, limit=10)
    payload = json.loads(out)
    assert len(payload) == 1


@pytest.mark.asyncio
async def test_append_user_notes_preview_truncates_long_text(confirm_setup):
    mgr, queue = confirm_setup
    text = "x" * 500

    async def h(req):
        if req.url.path == "/v1/wiki/node":
            return httpx.Response(200, json={
                "path": "/A", "etag": "e1", "user_notes": "prior\n"})
        if req.url.path == "/v1/wiki/user-notes":
            return httpx.Response(200, json={"etag": "e2"})
        return httpx.Response(404)

    wiki_skills.WIKI_CLIENT_VAR.set(_make_client(h))

    task = asyncio.create_task(
        wiki_skills._wiki_append_user_notes_impl("/A", text))
    for _ in range(20):
        await asyncio.sleep(0.01)
        if queue.events:
            break
    assert queue.events, "no confirm event was emitted"
    ev = queue.events[0]
    assert ev["type"] == "confirmation_required"
    assert "+ " + text[:200] + "…" in ev["command"]
    assert f"(+{500-200} more chars)" in ev["command"]
    mgr.resolve(ev["confirm_id"], True, expected_session_id="s1")
    out = await task
    assert json.loads(out)["ok"] is True


@pytest.mark.asyncio
async def test_append_user_notes_user_declines(confirm_setup):
    mgr, queue = confirm_setup

    async def h(req):
        if req.url.path == "/v1/wiki/node":
            return httpx.Response(200, json={"path": "/A", "etag": "e1", "user_notes": ""})
        if req.url.path == "/v1/wiki/user-notes":
            pytest.fail("PUT should not happen when user declines")
        return httpx.Response(500)

    wiki_skills.WIKI_CLIENT_VAR.set(_make_client(h))
    task = asyncio.create_task(
        wiki_skills._wiki_append_user_notes_impl("/A", "hi"))
    for _ in range(20):
        await asyncio.sleep(0.01)
        if queue.events:
            break
    mgr.resolve(queue.events[0]["confirm_id"], False, expected_session_id="s1")
    payload = json.loads(await task)
    assert payload["error"] == "user declined"


@pytest.mark.asyncio
async def test_append_etag_409_retries_once_then_succeeds(confirm_setup):
    mgr, queue = confirm_setup
    state = {"node_calls": 0, "put_calls": 0}

    async def h(req):
        if req.url.path == "/v1/wiki/node":
            state["node_calls"] += 1
            etag = "e1" if state["node_calls"] == 1 else "e2"
            return httpx.Response(200, json={"path": "/A", "etag": etag, "user_notes": "x"})
        if req.url.path == "/v1/wiki/user-notes":
            state["put_calls"] += 1
            if state["put_calls"] == 1:
                return httpx.Response(409, text="etag mismatch")
            return httpx.Response(200, json={"etag": "e3"})
        return httpx.Response(404)

    wiki_skills.WIKI_CLIENT_VAR.set(_make_client(h))
    task = asyncio.create_task(
        wiki_skills._wiki_append_user_notes_impl("/A", "more"))
    for _ in range(20):
        await asyncio.sleep(0.01)
        if queue.events:
            break
    mgr.resolve(queue.events[0]["confirm_id"], True, expected_session_id="s1")
    payload = json.loads(await task)
    assert payload["ok"] is True
    assert state["put_calls"] == 2
    assert state["node_calls"] == 2


@pytest.mark.asyncio
async def test_append_etag_409_twice_returns_error(confirm_setup):
    mgr, queue = confirm_setup

    async def h(req):
        if req.url.path == "/v1/wiki/node":
            return httpx.Response(200, json={"path": "/A", "etag": "e1", "user_notes": "x"})
        if req.url.path == "/v1/wiki/user-notes":
            return httpx.Response(409, text="etag mismatch")
        return httpx.Response(404)

    wiki_skills.WIKI_CLIENT_VAR.set(_make_client(h))
    task = asyncio.create_task(
        wiki_skills._wiki_append_user_notes_impl("/A", "more"))
    for _ in range(20):
        await asyncio.sleep(0.01)
        if queue.events:
            break
    mgr.resolve(queue.events[0]["confirm_id"], True, expected_session_id="s1")
    payload = json.loads(await task)
    assert payload["error"] == "etag conflict"


@pytest.mark.asyncio
async def test_replace_etag_409_does_not_retry(confirm_setup):
    mgr, queue = confirm_setup
    state = {"put_calls": 0}

    async def h(req):
        if req.url.path == "/v1/wiki/node":
            return httpx.Response(200, json={"path": "/A", "etag": "e1", "user_notes": "x"})
        if req.url.path == "/v1/wiki/user-notes":
            state["put_calls"] += 1
            return httpx.Response(409, text="etag mismatch")
        return httpx.Response(404)

    wiki_skills.WIKI_CLIENT_VAR.set(_make_client(h))
    task = asyncio.create_task(
        wiki_skills._wiki_replace_user_notes_impl("/A", "overwrite"))
    for _ in range(20):
        await asyncio.sleep(0.01)
        if queue.events:
            break
    mgr.resolve(queue.events[0]["confirm_id"], True, expected_session_id="s1")
    payload = json.loads(await task)
    assert "content modified by others" in payload["error"]
    assert state["put_calls"] == 1


@pytest.mark.asyncio
async def test_register_root_blocked_by_blacklist(confirm_setup):
    mgr, queue = confirm_setup
    wiki_skills.USER_PATTERNS_VAR.set(["secret/**"])

    async def h(req):
        pytest.fail("client must not be called when blacklisted")

    wiki_skills.WIKI_CLIENT_VAR.set(_make_client(h))
    out = await wiki_skills._wiki_register_root_impl("/secret/x", "project")
    assert json.loads(out)["error"] == "path is on user's hard blacklist"
    assert queue.events == []


@pytest.mark.asyncio
async def test_register_root_calls_post_roots(confirm_setup):
    mgr, queue = confirm_setup
    state = {"posted": None}

    async def h(req):
        if req.url.path == "/v1/wiki/roots":
            state["posted"] = json.loads(await req.aread())
            return httpx.Response(200, json={"id": "r-new", "path": "/x", "level": "project"})
        return httpx.Response(404)

    wiki_skills.WIKI_CLIENT_VAR.set(_make_client(h))
    task = asyncio.create_task(
        wiki_skills._wiki_register_root_impl("/x", "project"))
    for _ in range(20):
        await asyncio.sleep(0.01)
        if queue.events:
            break
    mgr.resolve(queue.events[0]["confirm_id"], True, expected_session_id="s1")
    payload = json.loads(await task)
    assert payload["ok"] is True
    assert state["posted"]["path"] == "/x"
    assert state["posted"]["level"] == "project"
