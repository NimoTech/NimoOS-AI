import asyncio
import os
import time
import pytest
import db as db_module
from fs import ops as fsops
from fs.snapshots import SnapshotStore
from confirm import ConfirmManager
from fs import access_request


class FakeSink:
    def __init__(self):
        self.events: list[dict] = []
    async def put(self, e):
        self.events.append(e)


@pytest.fixture
def ctx(tmp_path):
    conn = db_module.init_db(str(tmp_path / "a.db"),
                             snapshots_root=str(tmp_path / "snap"))
    now = int(time.time())
    conn.execute("INSERT INTO sessions (id, user_id, title, created_at, updated_at) "
                 "VALUES (?,?,?,?,?)", ("s1", "u1", None, now, now))
    root = tmp_path / "root"
    root.mkdir()
    conn.execute("INSERT INTO visible_resources (session_id, path, kind, added_at) "
                 "VALUES (?,?,?,?)", ("s1", str(root), "folder", now))
    conn.commit()
    sink = FakeSink()
    store = SnapshotStore(root=str(tmp_path / "snap"))
    access_request.reset_state()
    return {
        "conn": conn, "sink": sink, "store": store, "root": str(root),
        "session_id": "s1", "run_id": "r1", "user_patterns": [],
        "chat_username": "nobody-xyz",
        "confirm_mgr": ConfirmManager(conn),
    }


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_list_dir_returns_entries(ctx, tmp_path):
    p = tmp_path / "root" / "f.txt"; p.write_text("x")
    out = _run(fsops.list_dir(ctx, ctx["root"]))
    assert "f.txt" in out


def test_read_file_text(ctx, tmp_path):
    p = tmp_path / "root" / "a.txt"; p.write_text("hello")
    out = _run(fsops.read_file(ctx, str(p)))
    assert out == "hello"


def test_read_file_too_large(ctx, tmp_path):
    p = tmp_path / "root" / "big.txt"
    p.write_bytes(b"x" * (2 * 1024 * 1024))
    out = _run(fsops.read_file(ctx, str(p)))
    assert "too large" in out.lower()


def test_write_file_stages(ctx, tmp_path):
    p = tmp_path / "root" / "new.txt"
    msg = _run(fsops.write_file(ctx, str(p), "yo"))
    assert "Staged" in msg
    assert p.read_text() == "yo"
    assert any(e["type"] == "staged_change" for e in ctx["sink"].events)


def test_edit_file_unique_match(ctx, tmp_path):
    p = tmp_path / "root" / "x.txt"; p.write_text("alpha\nbeta\n")
    msg = _run(fsops.edit_file(ctx, str(p), "alpha", "ALPHA"))
    assert p.read_text() == "ALPHA\nbeta\n"


def test_edit_file_nonunique_rejected(ctx, tmp_path):
    p = tmp_path / "root" / "x.txt"; p.write_text("a\na\n")
    msg = _run(fsops.edit_file(ctx, str(p), "a", "Z"))
    assert "not unique" in msg.lower()


def test_delete_file(ctx, tmp_path):
    p = tmp_path / "root" / "g.txt"; p.write_text("y")
    _run(fsops.delete_path(ctx, str(p)))
    assert not p.exists()


def test_delete_nonempty_dir_requires_recursive(ctx, tmp_path):
    d = tmp_path / "root" / "d"; d.mkdir()
    (d / "f").write_text("x")
    msg = _run(fsops.delete_path(ctx, str(d)))
    assert "recursive" in msg.lower()


def test_rename_rejected_when_dst_exists(ctx, tmp_path):
    a = tmp_path / "root" / "a.txt"; a.write_text("a")
    b = tmp_path / "root" / "b.txt"; b.write_text("b")
    msg = _run(fsops.rename(ctx, str(a), str(b)))
    assert "already exists" in msg.lower()


def test_glob_returns_matches(ctx, tmp_path):
    (tmp_path / "root" / "x.py").write_text("p")
    (tmp_path / "root" / "y.py").write_text("p")
    out = _run(fsops.glob_files(ctx, "*.py", ctx["root"]))
    assert "x.py" in out and "y.py" in out


def test_search_content(ctx, tmp_path):
    (tmp_path / "root" / "a.txt").write_text("hello world\n")
    out = _run(fsops.search_content(ctx, "hello", ctx["root"], None))
    assert "a.txt" in out


def test_scope_violation(ctx, tmp_path):
    other = tmp_path / "other"; other.mkdir()
    (other / "x.txt").write_text("x")

    async def go():
        task = asyncio.ensure_future(fsops.read_file(ctx, str(other / "x.txt")))
        await _resolve_event(ctx, False)
        return await task
    out = _run(go())
    assert "Error" in out


def _resolve_event(ctx, confirmed):
    async def waiter():
        for _ in range(200):
            evs = [e for e in ctx["sink"].events if e["type"] == "access_request"]
            if evs:
                ctx["confirm_mgr"].resolve(evs[-1]["confirm_id"], confirmed,
                                           expected_session_id=ctx["session_id"])
                return
            await asyncio.sleep(0.005)
        raise AssertionError("no access_request event")
    return waiter()


def test_unauthorized_dir_grant_then_list(ctx, tmp_path):
    # A dir OUTSIDE the visible root.
    outside = tmp_path / "outside"; outside.mkdir()
    (outside / "f.txt").write_text("x")

    async def go():
        task = asyncio.ensure_future(fsops.list_dir(ctx, str(outside)))
        await _resolve_event(ctx, True)
        return await task
    out = _run(go())
    assert "f.txt" in out  # op continued after grant
    assert ctx["conn"].execute(
        "SELECT kind FROM visible_resources WHERE path=?", (str(outside),)
    ).fetchone()["kind"] == "folder"


def test_unauthorized_deny_returns_error(ctx, tmp_path):
    outside = tmp_path / "nope"; outside.mkdir()

    async def go():
        task = asyncio.ensure_future(fsops.list_dir(ctx, str(outside)))
        await _resolve_event(ctx, False)
        return await task
    out = _run(go())
    assert "The user denied access to" in out


def test_hard_blacklist_never_prompts(ctx):
    out = _run(fsops.read_file(ctx, "/etc/passwd"))
    assert out.startswith("Error:")
    assert not [e for e in ctx["sink"].events if e["type"] == "access_request"]


def test_new_file_requests_nearest_existing_folder(ctx, tmp_path):
    # /existing exists, /existing/new.txt does not.
    existing = tmp_path / "existing"; existing.mkdir()
    target = existing / "new.txt"

    async def go():
        task = asyncio.ensure_future(fsops.write_file(ctx, str(target), "hi"))
        await _resolve_event(ctx, True)
        return await task
    out = _run(go())
    assert "Staged" in out
    row = ctx["conn"].execute(
        "SELECT path, kind FROM visible_resources WHERE path=?", (str(existing),)
    ).fetchone()
    assert row is not None and row["kind"] == "folder"  # folder, not the file


def test_blacklisted_directory_itself_never_prompts(ctx):
    # Requesting a hard-blacklisted directory itself (not a child) must not
    # produce a card, even though it's a real existing directory.
    out = _run(fsops.list_dir(ctx, "/etc"))
    assert out.startswith("Error:")
    assert not [e for e in ctx["sink"].events if e["type"] == "access_request"]


def test_nonexistent_top_level_never_prompts(ctx):
    # Path whose only existing ancestor is "/" must never request "/".
    out = _run(fsops.list_dir(ctx, "/__no_such_top__/a/b"))
    assert out.startswith("Error:")
    assert not [e for e in ctx["sink"].events if e["type"] == "access_request"]


def test_no_confirm_manager_errors_immediately(ctx, tmp_path):
    # Headless / no interactive channel: out-of-scope path errors at once,
    # no hang, no card.
    ctx["confirm_mgr"] = None
    outside = tmp_path / "headless"; outside.mkdir()
    out = _run(fsops.list_dir(ctx, str(outside)))
    assert out.startswith("Error:")
    assert not [e for e in ctx["sink"].events if e["type"] == "access_request"]
