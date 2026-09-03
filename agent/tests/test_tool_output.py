# tests/test_tool_output.py
import os
import re
import time

import pytest

import tool_output as to
from db import init_db


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "t.db"))
    c.execute("INSERT INTO sessions(id,user_id,created_at,updated_at,source) "
              "VALUES('chat1','u1',0,0,'web')")
    c.execute("INSERT INTO sessions(id,user_id,created_at,updated_at,source) "
              "VALUES('task1','u1',0,0,'task')")
    c.execute("INSERT INTO scheduled_tasks(id,user_id,name,prompt,agent_type,trigger_type,"
              "webhook_token,created_at,updated_at) "
              "VALUES('tid1','u1','n','p','general','cron','wh1',0,0)")
    c.execute("INSERT INTO task_runs(id,task_id,user_id,session_id,trigger,status,created_at) "
              "VALUES('r1','tid1','u1','task1','cron','running',1)")
    c.commit()
    return c


@pytest.fixture
def roots(tmp_path, monkeypatch):
    chat_root = tmp_path / "chat-root"
    ws_root = tmp_path / "ws-root"
    monkeypatch.setattr(to, "ROOT", str(chat_root))
    from tasks import workspace
    monkeypatch.setattr(workspace, "ROOT", str(ws_root))
    return chat_root, ws_root


def test_chat_session_dir_is_root_slash_session(conn, roots):
    chat_root, _ = roots
    assert to.resolve_offload_dir(conn, "chat1") == str(chat_root / "chat1")


def test_task_session_dir_is_workspace_subdir(conn, roots):
    _, ws_root = roots
    assert to.resolve_offload_dir(conn, "task1") == str(ws_root / "tid1" / ".tool-outputs")


def test_unknown_session_falls_back_to_chat_dir(conn, roots):
    chat_root, _ = roots
    assert to.resolve_offload_dir(conn, "nope") == str(chat_root / "nope")


def test_ensure_creates_dir_and_never_raises(conn, roots, monkeypatch):
    d = to.ensure_offload_dir(conn, "chat1")
    assert os.path.isdir(d)
    monkeypatch.setattr(to.os, "makedirs", lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    assert to.ensure_offload_dir(conn, "chat1") == ""


def test_store_writes_raw_text_under_offload_dir(tmp_path):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    p = to.store_output("hello\nworld", call_id="call_abc-1", tool_name="web_fetch")
    assert p == str(tmp_path / "call_abc-1.txt")
    assert open(p, encoding="utf-8").read() == "hello\nworld"


def test_store_cleans_up_tmp_on_any_write_exception(tmp_path, monkeypatch):
    """Minor: store_output must catch any write-time exception (not just
    OSError) so a stray tmp file never survives a failed store."""
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    real_open = open

    def boom(path, *a, **k):
        if str(path).endswith(".tmp"):
            raise ValueError("simulated non-OSError write failure")
        return real_open(path, *a, **k)

    monkeypatch.setattr(to, "open", boom, raising=False)
    assert to.store_output("hello", call_id="call_boom") == ""
    assert list(tmp_path.iterdir()) == []


def test_store_refuses_unsafe_call_id_or_missing_dir(tmp_path):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    assert to.store_output("x", call_id="../evil") == ""
    assert to.store_output("x", call_id="") == ""
    to.OFFLOAD_DIR_VAR.set("")
    assert to.store_output("x", call_id="ok") == ""


def test_placeholder_has_fenced_preview_and_exact_trailer():
    text = "A" * 2000 + "B" * 5000 + "C" * 400
    ph = to.make_placeholder(text, tool_name="web_fetch", path="/x/y/c1.txt", chars=len(text))
    assert ph.startswith('<untrusted-data source="tool-output-preview">')
    assert "A" * 1500 in ph
    assert "C" * 300 in ph
    assert "B" * 1000 not in ph
    m = to.TRAILER_RE.search(ph)
    assert m and m.group(1) == str(len(text)) and m.group(2) == "/x/y/c1.txt"
    assert "read_file_lines" in ph and "web_fetch" in ph


def test_placeholder_advice_mentions_expand_tools_and_search_content_no_reoffload():
    text = "A" * 2000 + "B" * 5000 + "C" * 400
    ph = to.make_placeholder(text, tool_name="web_fetch", path="/x/y/c1.txt", chars=len(text))
    assert 'expand_tools(["files"])' in ph
    assert "read_file_lines" in ph
    assert "search_content(query, root=" in ph
    assert "Do not re-run the tool" in ph


def test_postprocess_small_and_non_str_pass_through(tmp_path):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    assert to.postprocess("short", tool_name="t", call_id="c1") == "short"
    obj = {"not": "str"}
    assert to.postprocess(obj, tool_name="t", call_id="c1") is obj
    assert to.postprocess(None, tool_name="t", call_id="c1") is None


def test_postprocess_offloads_large_output(tmp_path):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    big = "x" * (to.OFFLOAD_THRESHOLD_CHARS + 1)
    out = to.postprocess(big, tool_name="read_file", call_id="call_1")
    assert out != big
    assert to.TRAILER_RE.search(out)
    assert open(tmp_path / "call_1.txt", encoding="utf-8").read() == big


def test_postprocess_keeps_output_when_store_fails(tmp_path):
    to.OFFLOAD_DIR_VAR.set("")          # no folder for this run → cannot store
    big = "x" * (to.OFFLOAD_THRESHOLD_CHARS + 1)
    assert to.postprocess(big, tool_name="t", call_id="c") == big


def test_postprocess_is_idempotent_on_placeholder(tmp_path):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    big = "x" * (to.OFFLOAD_THRESHOLD_CHARS + 1)
    once = to.postprocess(big, tool_name="t", call_id="c2")
    # pad so the placeholder itself would be over threshold if it were treated as new output
    padded = once + "\n" + ("y" * to.OFFLOAD_THRESHOLD_CHARS)
    assert to.postprocess(padded, tool_name="t", call_id="c3") == padded


def test_is_offload_path_covers_chat_root_and_task_subdir(roots):
    chat_root, ws_root = roots
    assert to.is_offload_path(str(chat_root / "s1" / "c.txt"))
    assert to.is_offload_path(str(ws_root / "tid" / ".tool-outputs" / "c.txt"))
    assert not to.is_offload_path(str(ws_root / "tid" / "seen.json"))
    assert not to.is_offload_path("/DATA/Documents/a.txt")


def test_sweep_removes_only_expired_files(roots):
    chat_root, ws_root = roots
    old = chat_root / "s1" / "old.txt"; old.parent.mkdir(parents=True)
    old.write_text("o"); os.utime(old, (1, 1))
    new = chat_root / "s1" / "new.txt"; new.write_text("n")
    told = ws_root / "tid" / ".tool-outputs" / "old.txt"; told.parent.mkdir(parents=True)
    told.write_text("o"); os.utime(told, (1, 1))
    keep = ws_root / "tid" / "seen.json"; keep.write_text("k"); os.utime(keep, (1, 1))
    n = to.sweep_expired(now=time.time())
    assert n == 2
    assert not old.exists() and new.exists()
    assert not told.exists() and keep.exists()


def test_postprocess_exempts_search_photos(tmp_path):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    big = "x" * (to.OFFLOAD_THRESHOLD_CHARS + 1)
    out = to.postprocess(big, tool_name="search_photos", call_id="c1")
    assert out == big
    assert not (tmp_path / "c1.txt").exists()


def test_postprocess_exempts_mcp_error_prefix(tmp_path):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    big = "[MCP error] " + "x" * (to.OFFLOAD_THRESHOLD_CHARS + 1)
    out = to.postprocess(big, tool_name="some_mcp_tool", call_id="c2")
    assert out == big
    assert not (tmp_path / "c2.txt").exists()


def test_postprocess_still_offloads_web_fetch(tmp_path):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    big = "x" * (to.OFFLOAD_THRESHOLD_CHARS + 1)
    out = to.postprocess(big, tool_name="web_fetch", call_id="c3")
    assert out != big
    assert to.TRAILER_RE.search(out)


def test_safe_call_id():
    assert to.is_safe_call_id("call_v9zq0zqqrwyalonct0vyu351")
    assert not to.is_safe_call_id("a/b") and not to.is_safe_call_id("") and not to.is_safe_call_id("x" * 200)
