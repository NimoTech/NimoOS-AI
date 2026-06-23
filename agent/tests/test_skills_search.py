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


@pytest.mark.asyncio
async def test_read_document_path_authorized_calls_parser(monkeypatch, tmp_path):
    conn, root = _fs_authorized_conn(tmp_path)
    f = root / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    fsskill.SESSION_ID_VAR.set("s1")
    fsskill.DB_VAR.set(conn)
    fsskill.USER_PATTERNS_VAR.set([])
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
    fsskill.SESSION_ID_VAR.set("s1")
    fsskill.DB_VAR.set(conn)
    fsskill.USER_PATTERNS_VAR.set([])
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
async def test_read_document_no_args_errors():
    out = await search_skill._read_document_impl()
    assert "error" in json.loads(out)


@pytest.mark.asyncio
async def test_read_document_path_no_run_context_errors(monkeypatch):
    # Run inside a fresh copy_context so SESSION_ID_VAR / DB_VAR are guaranteed
    # unset (no default) — their .get() raises LookupError, which must be caught
    # inside the try block and returned as error JSON rather than escaping.
    import asyncio
    import contextvars

    called = {"n": 0}
    async def fake_extract(*a, **k):
        called["n"] += 1
        return {}
    monkeypatch.setattr(search_skill._parser_client, "extract", fake_extract)

    result_holder = {}
    async def _run():
        # Fresh context: SESSION_ID_VAR / DB_VAR are unset → .get() raises LookupError
        out = await search_skill._read_document_impl(path="/DATA/x.pdf")
        result_holder["out"] = out

    ctx = contextvars.copy_context()
    # Run _run in a context where the fs vars were never set.
    # We use loop.run_in_executor with the context, or simply run directly since
    # copy_context() inherits current values — instead create a truly empty
    # context by resetting any set tokens first.
    session_tok = search_skill._fsskill.SESSION_ID_VAR.set("_sentinel_to_reset")
    db_tok = search_skill._fsskill.DB_VAR.set("_sentinel_to_reset")
    search_skill._fsskill.SESSION_ID_VAR.reset(session_tok)
    search_skill._fsskill.DB_VAR.reset(db_tok)

    await _run()

    assert "error" in json.loads(result_holder["out"])
    assert called["n"] == 0
