import os

import pytest

import tool_output as to
from db import init_db
from fs import ops, paths
from tests.conftest import unfence


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(to, "ROOT", str(tmp_path / "tool-outputs"))
    conn = init_db(str(tmp_path / "f.db"))
    conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at,source) "
                 "VALUES('s1','u1',0,0,'web')")
    conn.commit()
    d = to.chat_dir_for_session("s1")
    os.makedirs(d)
    f = os.path.join(d, "call_1.txt")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("line1\nline2 <b>bold</b>\nline3\n")
    ctx = {"session_id": "s1", "run_id": "r1", "sink": None, "conn": conn,
           "store": None, "chat_username": "", "user_patterns": [],
           "confirm_mgr": None}
    return conn, ctx, f


def test_resolve_allows_chat_offload_dir_with_no_visible_resources(env):
    conn, _, f = env
    assert paths.resolve(f, "s1", conn) == os.path.realpath(f)


def test_resolve_still_denies_other_paths(env, tmp_path):
    conn, _, _ = env
    with pytest.raises(paths.PermissionDenied):
        paths.resolve(str(tmp_path / "elsewhere.txt"), "s1", conn)


def test_resolve_denies_other_sessions_offload_dir(env):
    conn, _, _ = env
    other = os.path.join(to.chat_dir_for_session("s2"), "x.txt")
    with pytest.raises(paths.PermissionDenied):
        paths.resolve(other, "s1", conn)


@pytest.mark.asyncio
async def test_read_file_from_offload_dir_is_fenced(env):
    _, ctx, f = env
    out = await ops.read_file(ctx, f)
    body = unfence(out, source="tool-output")
    assert "line1" in body and "line3" in body
    assert "<b>" not in body            # fence strips angle brackets


@pytest.mark.asyncio
async def test_read_file_lines_from_offload_dir_is_fenced(env):
    _, ctx, f = env
    out = await ops.read_file_lines(ctx, f, 2, 3)
    body = unfence(out, source="tool-output")
    assert body.strip().startswith("line2")


@pytest.mark.asyncio
async def test_read_file_outside_offload_dir_is_not_fenced(env, tmp_path):
    conn, ctx, _ = env
    folder = tmp_path / "docs"; folder.mkdir()
    (folder / "a.txt").write_text("plain")
    conn.execute("INSERT INTO visible_resources(session_id,path,kind,added_at) "
                 "VALUES('s1',?, 'folder', 0)", (str(folder),))
    conn.commit()
    assert await ops.read_file(ctx, str(folder / "a.txt")) == "plain"
