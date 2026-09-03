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


def test_resolve_denies_symlinked_offload_dir_escaping_root(env, tmp_path):
    """F2: chat_dir_for_session("s-link") being a symlink to somewhere outside
    ROOT must not grant access to that outside directory. Before the fix,
    `own = realpath(chat_dir_for_session(session_id))` follows the symlink and
    the containment check compares against the (now-outside) target, so any
    path under the outside directory passed. The fix additionally requires
    containment in realpath(ROOT)."""
    conn, _, _ = env
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("top secret")
    os.symlink(str(outside), to.chat_dir_for_session("s-link"))
    with pytest.raises(paths.PermissionDenied):
        paths.resolve(str(secret), "s-link", conn)


def test_resolve_still_allows_real_offload_dir_under_root(env):
    """Sanity check that F2's extra containment check doesn't break the
    ordinary (non-symlinked) case exercised above."""
    conn, _, f = env
    assert paths.resolve(f, "s1", conn) == os.path.realpath(f)


@pytest.mark.asyncio
async def test_read_file_from_offload_dir_is_fenced(env):
    _, ctx, f = env
    out = await ops.read_file(ctx, f)
    body = unfence(out, source="tool-output")
    assert "line1" in body and "line3" in body
    assert "<b>" not in body            # fence strips angle brackets


@pytest.mark.asyncio
async def test_read_file_lines_whitespace_only_slice_not_emptied(env):
    """Minor: fence_untrusted returns "" for whitespace-only content (its
    `if not body.strip(): return ""` guard), so _fence_if_offload must fall
    back to the original text rather than silently turning a real (blank)
    line range into an empty string."""
    _, ctx, f = env
    d = os.path.dirname(f)
    ws_path = os.path.join(d, "call_ws.txt")
    with open(ws_path, "w", encoding="utf-8") as fh:
        fh.write("line1\n\n   \nline4\n")
    out = await ops.read_file_lines(ctx, ws_path, 2, 3)
    assert out == "\n"


@pytest.mark.asyncio
async def test_read_file_lines_from_offload_dir_is_fenced(env):
    _, ctx, f = env
    out = await ops.read_file_lines(ctx, f, 2, 3)
    body = unfence(out, source="tool-output")
    assert body.strip().startswith("line2")


@pytest.mark.asyncio
async def test_read_file_on_large_offload_file_returns_head_not_full_text(env):
    _, ctx, f = env
    d = os.path.dirname(f)
    big_path = os.path.join(d, "call_big.txt")
    big_text = "L" * 20000
    with open(big_path, "w", encoding="utf-8") as fh:
        fh.write(big_text)
    out = await ops.read_file(ctx, big_path)
    assert "[offloaded result:" in out
    assert big_path in out
    assert len(out) < 3000
    # the fenced head should not contain the full 20000-char body
    assert "L" * 20000 not in out


@pytest.mark.asyncio
async def test_read_file_on_small_offload_file_still_returns_full_content(env):
    _, ctx, f = env
    out = await ops.read_file(ctx, f)
    body = unfence(out, source="tool-output")
    assert "line1" in body and "line3" in body
    assert "[offloaded result:" not in out


@pytest.mark.asyncio
async def test_read_file_outside_offload_dir_is_not_fenced(env, tmp_path):
    conn, ctx, _ = env
    folder = tmp_path / "docs"; folder.mkdir()
    (folder / "a.txt").write_text("plain")
    conn.execute("INSERT INTO visible_resources(session_id,path,kind,added_at) "
                 "VALUES('s1',?, 'folder', 0)", (str(folder),))
    conn.commit()
    assert await ops.read_file(ctx, str(folder / "a.txt")) == "plain"
