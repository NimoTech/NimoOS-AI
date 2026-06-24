import json

import httpx
import pytest

from skills import ALL_TOOLS
from skills.search import search as search_skill


def test_search_tools_in_all_tools():
    tool_names = {t.name for t in ALL_TOOLS}
    assert "nimoos_search" in tool_names
    assert "read_file_chunk" in tool_names


@pytest.fixture(autouse=True)
def _reset_search_user_id():
    # Each test starts with the var at its default; set explicitly per test.
    search_skill.USER_ID_VAR.set("")
    yield


@pytest.mark.asyncio
async def test_nimoos_search_propagates_user_id(monkeypatch):
    calls = {}

    async def fake_invoke(name, arguments, user_id=None):
        calls["name"] = name
        calls["arguments"] = arguments
        calls["user_id"] = user_id
        return {"hits": []}

    monkeypatch.setattr(search_skill._client, "invoke_tool", fake_invoke)
    search_skill.USER_ID_VAR.set("u1")
    out = await search_skill._nimoos_search_impl("hello", top_k=3)
    assert calls["user_id"] == "u1"
    assert calls["name"] == "nimoos_search"
    assert calls["arguments"]["query"] == "hello"
    assert calls["arguments"]["top_k"] == 3
    assert "modality" not in calls["arguments"], "modality removed in favor of sources"
    assert "sources" not in calls["arguments"], "sources absent when not specified"
    assert json.loads(out) == {"hits": []}


@pytest.mark.asyncio
async def test_nimoos_search_user_id_none_when_unset(monkeypatch):
    captured = {}

    async def fake_invoke(name, arguments, user_id=None):
        captured["user_id"] = user_id
        return {"hits": []}

    monkeypatch.setattr(search_skill._client, "invoke_tool", fake_invoke)
    # USER_ID_VAR left at "" by the autouse fixture.
    await search_skill._nimoos_search_impl("hi")
    assert captured["user_id"] is None


@pytest.mark.asyncio
async def test_read_file_chunk_propagates_user_id(monkeypatch):
    captured = {}

    async def fake_invoke(name, arguments, user_id=None):
        captured["name"] = name
        captured["arguments"] = arguments
        captured["user_id"] = user_id
        return {"chunks": []}

    monkeypatch.setattr(search_skill._client, "invoke_tool", fake_invoke)
    search_skill.USER_ID_VAR.set("u2")
    await search_skill._read_file_chunk_impl("f1", "body", 0, window=3)
    assert captured["user_id"] == "u2"
    assert captured["name"] == "read_file_chunk"
    assert captured["arguments"] == {
        "file_id": "f1", "kind": "body", "chunk_no": 0, "window": 3,
    }


@pytest.mark.asyncio
async def test_nimoos_search_returns_error_json_on_http_error(monkeypatch):
    async def fake_invoke(name, arguments, user_id=None):
        raise httpx.HTTPStatusError(
            "400 Bad Request",
            request=httpx.Request("POST", "http://x"),
            response=httpx.Response(400),
        )

    monkeypatch.setattr(search_skill._client, "invoke_tool", fake_invoke)
    out = await search_skill._nimoos_search_impl("hi")
    data = json.loads(out)
    assert "error" in data


@pytest.mark.asyncio
async def test_nimoos_search_returns_error_json_on_bad_filters(monkeypatch):
    async def fake_invoke(name, arguments, user_id=None):  # pragma: no cover
        raise AssertionError("should not reach client with bad filters")

    monkeypatch.setattr(search_skill._client, "invoke_tool", fake_invoke)
    out = await search_skill._nimoos_search_impl("hi", filters="{not json")
    data = json.loads(out)
    assert "error" in data


@pytest.mark.asyncio
async def test_read_file_chunk_returns_error_json_on_http_error(monkeypatch):
    async def fake_invoke(name, arguments, user_id=None):
        raise httpx.HTTPError("timeout")

    monkeypatch.setattr(search_skill._client, "invoke_tool", fake_invoke)
    out = await search_skill._read_file_chunk_impl("f1", "body", 0)
    assert "error" in json.loads(out)


def test_agent_imports_search_skills_module():
    # Guards the new `import skills.search as search_skills` in agent.py and
    # that agent.py references the same USER_ID_VAR object the skill reads.
    import agent as agent_module
    from skills.search import search as search_skill
    assert agent_module.search_skills.USER_ID_VAR is search_skill.USER_ID_VAR


def test_read_document_in_all_tools():
    from skills import ALL_TOOLS
    assert "read_document" in {t.name for t in ALL_TOOLS}


@pytest.mark.asyncio
async def test_read_document_propagates_user_id_and_args(monkeypatch):
    calls = {}

    async def fake_invoke(name, arguments, user_id=None):
        calls["name"] = name
        calls["arguments"] = arguments
        calls["user_id"] = user_id
        return {"file_id": "f1", "text": "hello [Page 1]", "truncated": False,
                "total_chars": 14, "next_offset": 0}

    monkeypatch.setattr(search_skill._client, "invoke_tool", fake_invoke)
    search_skill.USER_ID_VAR.set("u1")
    out = await search_skill._read_document_impl("f1", offset=0, max_chars=24000)
    assert calls["name"] == "read_document"
    assert calls["user_id"] == "u1"
    assert calls["arguments"] == {"file_id": "f1", "offset": 0, "max_chars": 24000}
    data = json.loads(out)
    assert data["text"] == "hello [Page 1]"
    assert data["truncated"] is False


@pytest.mark.asyncio
async def test_read_document_handles_http_error(monkeypatch):
    async def fake_invoke(name, arguments, user_id=None):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(search_skill._client, "invoke_tool", fake_invoke)
    out = await search_skill._read_document_impl("f1")
    assert "error" in json.loads(out)


import os
import time
import db as db_module
from skills import filesystem as fsskill


def _fs_authorized_conn(tmp_path):
    conn = db_module.init_db(str(tmp_path / "fs.db"),
                             snapshots_root=str(tmp_path / "snap"))
    now = int(time.time())
    conn.execute("INSERT INTO sessions (id, user_id, title, created_at, updated_at) "
                 "VALUES (?,?,?,?,?)", ("s1", "u1", None, now, now))
    root = tmp_path / "root"
    root.mkdir()
    conn.execute("INSERT INTO visible_resources (session_id, path, kind, added_at) "
                 "VALUES (?,?,?,?)", ("s1", str(root), "folder", now))
    conn.commit()
    return conn, root


class _FakeSink:
    def __init__(self):
        self.events = []

    async def put(self, e):
        self.events.append(e)


def _set_full_fs_ctx(conn, *, confirm_mgr=None, sink=None):
    """Set ALL filesystem ContextVars read_document/view_document_page rely on
    via fsskill._ctx(). RUN_ID/EVENT_QUEUE/STORE have no default, so they must
    be set or _ctx() raises LookupError. confirm_mgr=None means no interactive
    channel (out-of-scope paths are rejected without a card)."""
    fsskill.SESSION_ID_VAR.set("s1")
    fsskill.RUN_ID_VAR.set("r1")
    fsskill.DB_VAR.set(conn)
    fsskill.USER_PATTERNS_VAR.set([])
    fsskill.EVENT_QUEUE_VAR.set(sink if sink is not None else _FakeSink())
    fsskill.STORE_VAR.set(None)
    fsskill.CONFIRM_MGR_VAR.set(confirm_mgr)


@pytest.mark.asyncio
async def test_read_document_path_authorized_calls_parser(monkeypatch, tmp_path):
    conn, root = _fs_authorized_conn(tmp_path)
    f = root / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    _set_full_fs_ctx(conn)
    search_skill.USER_ID_VAR.set("u1")

    captured = {}
    async def fake_extract(path, ocr=False, max_chars=24000, user_id=None):
        captured.update(path=path, ocr=ocr, user_id=user_id)
        return {"path": path, "markdown": "hello pdf", "truncated": False, "ocr": ocr}

    monkeypatch.setattr(search_skill._parser_client, "extract", fake_extract)
    out = await search_skill._read_document_impl(path=str(f))
    data = json.loads(out)
    assert data["markdown"] == "hello pdf"
    assert captured["path"] == os.path.realpath(str(f))
    assert captured["user_id"] == "u1"


@pytest.mark.asyncio
async def test_read_document_path_unauthorized_is_blocked(monkeypatch, tmp_path):
    conn, _root = _fs_authorized_conn(tmp_path)
    _set_full_fs_ctx(conn)
    search_skill.USER_ID_VAR.set("u1")

    called = {"n": 0}
    async def fake_extract(*a, **k):
        called["n"] += 1
        return {}
    monkeypatch.setattr(search_skill._parser_client, "extract", fake_extract)

    out = await search_skill._read_document_impl(path="/etc/passwd")
    assert "error" in json.loads(out)
    assert called["n"] == 0  # parser never called for an unauthorized path


@pytest.mark.asyncio
async def test_read_document_path_out_of_scope_requests_access(monkeypatch, tmp_path):
    # An out-of-scope (but not blacklisted) path, WITH an interactive channel,
    # must pop the access-request card (request_access) and — once granted —
    # proceed to extract. This is the behavior that was missing.
    from fs import access_request as ar_mod
    conn, _root = _fs_authorized_conn(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    f = outside / "resume.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    _set_full_fs_ctx(conn, confirm_mgr=object())  # non-None → interactive
    search_skill.USER_ID_VAR.set("u1")

    recorded = {}
    async def fake_request_access(ctx, abs_path, kind, op):
        recorded.update(abs_path=abs_path, op=op)
        # simulate the user granting: persist so the retry resolve() succeeds
        conn.execute(
            "INSERT INTO visible_resources (session_id, path, kind, added_at) "
            "VALUES (?,?,?,?)", ("s1", abs_path, kind, 0))
        conn.commit()
        return True
    monkeypatch.setattr(ar_mod, "request_access", fake_request_access)

    captured = {}
    async def fake_extract(path, ocr=False, max_chars=24000, user_id=None):
        captured["path"] = path
        return {"path": path, "markdown": "resume text", "truncated": False, "ocr": ocr}
    monkeypatch.setattr(search_skill._parser_client, "extract", fake_extract)

    out = await search_skill._read_document_impl(path=str(f))
    data = json.loads(out)
    assert data["markdown"] == "resume text"            # proceeded after grant
    assert recorded["abs_path"] == os.path.realpath(str(f))  # card was requested
    assert recorded["op"] == "read"
    assert captured["path"] == os.path.realpath(str(f))


@pytest.mark.asyncio
async def test_read_document_no_args_errors():
    out = await search_skill._read_document_impl()
    assert "error" in json.loads(out)


@pytest.mark.asyncio
async def test_read_document_path_no_run_context_errors(monkeypatch):
    # When there is no active run context, fsskill._ctx() raises LookupError
    # (SESSION_ID/RUN_ID/etc. have no default). That must be caught inside the
    # try and returned as error JSON — never escape, never reach Parser.
    def _boom():
        raise LookupError("no active run context")
    monkeypatch.setattr(search_skill._fsskill, "_ctx", _boom)

    called = {"n": 0}
    async def fake_extract(*a, **k):
        called["n"] += 1
        return {}
    monkeypatch.setattr(search_skill._parser_client, "extract", fake_extract)

    out = await search_skill._read_document_impl(path="/DATA/x.pdf")
    assert "error" in json.loads(out)
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_view_document_page_renders_and_describes(monkeypatch, tmp_path):
    from skills import photos as photos_skill
    conn, root = _fs_authorized_conn(tmp_path)
    f = root / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    _set_full_fs_ctx(conn)
    search_skill.USER_ID_VAR.set("u1")
    photos_skill.VISION_CFG_VAR.set({"ok": True, "base_url": "x", "api_key": "k", "model": "m"})

    captured = {}
    async def fake_render(path, page_start, page_end, scale=2.0, user_id=None):
        captured.update(path=path, page=page_start, user_id=user_id)
        return {"path": path, "pages": [{"page": page_start, "png_b64": "IMG"}]}
    monkeypatch.setattr(search_skill._parser_client, "render_pages", fake_render)

    async def fake_describe(png_b64, prompt, mime="image/png"):
        captured["png"] = png_b64
        return "a bar chart of Q1 sales", None
    monkeypatch.setattr(search_skill._photos, "describe_image", fake_describe)

    out = await search_skill._view_document_page_impl(path=str(f), page=3)
    data = json.loads(out)
    assert data["description"] == "a bar chart of Q1 sales"
    assert captured["path"] == os.path.realpath(str(f))
    assert captured["page"] == 3
    assert captured["png"] == "IMG"


@pytest.mark.asyncio
async def test_view_document_page_no_vision_errors(monkeypatch, tmp_path):
    from skills import photos as photos_skill
    photos_skill.VISION_CFG_VAR.set({"ok": False})
    called = {"n": 0}
    async def fake_render(*a, **k):
        called["n"] += 1
        return {}
    monkeypatch.setattr(search_skill._parser_client, "render_pages", fake_render)
    out = await search_skill._view_document_page_impl(path="/DATA/x.pdf", page=1)
    assert "error" in json.loads(out)
    assert called["n"] == 0  # never rendered when model has no vision


@pytest.mark.asyncio
async def test_view_document_page_unauthorized_path(monkeypatch, tmp_path):
    from skills import photos as photos_skill
    conn, _root = _fs_authorized_conn(tmp_path)
    _set_full_fs_ctx(conn)
    photos_skill.VISION_CFG_VAR.set({"ok": True, "base_url": "x", "api_key": "k", "model": "m"})
    called = {"n": 0}
    async def fake_render(*a, **k):
        called["n"] += 1
        return {}
    monkeypatch.setattr(search_skill._parser_client, "render_pages", fake_render)
    out = await search_skill._view_document_page_impl(path="/etc/passwd", page=1)
    assert "error" in json.loads(out)
    assert called["n"] == 0  # never rendered for an unauthorized path
