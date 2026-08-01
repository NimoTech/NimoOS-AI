import time

import pytest

import db as db_module
import skills.filesystem as fs_skill
from skills.send_attachment import _default_validate, _send_attachment_impl
from skills import send_attachment as send_attachment_mod


@pytest.mark.asyncio
async def test_send_ok_returns_real_success():
    sent = {}
    async def fake_send(path, caption): sent["p"] = path; return "mid42"
    out = await _send_attachment_impl("/DATA/x.txt", "hi", send_file=fake_send,
                                      validate=lambda p: "/DATA/x.txt")
    assert "mid42" in out and sent["p"] == "/DATA/x.txt"


@pytest.mark.asyncio
async def test_send_rejects_out_of_gate():
    out = await _send_attachment_impl("/etc/passwd", "", send_file=None,
                                      validate=lambda p: None)  # gate denies
    assert "error" in out.lower() or "not allowed" in out.lower()


@pytest.mark.asyncio
async def test_send_failure_surfaces_to_model():
    async def boom(path, caption): raise RuntimeError("network down")
    out = await _send_attachment_impl("/DATA/x.txt", "", send_file=boom,
                                      validate=lambda p: "/DATA/x.txt")
    assert "network down" in out or "fail" in out.lower()


# ---------------------------------------------------------------------------
# Real fs-gate integration tests for _default_validate.
#
# These exercise the ACTUAL authorization boundary (fs.ops._resolve_and_gate
# -> fs.paths.resolve -> visible_resources scope check), not an injected
# fake `validate`, so a future gate refactor that silently breaks the
# integration will be caught here. Setup mirrors tests/test_fs_paths.py and
# tests/test_read_attachment_tool.py::test_function_tool_reads_via_context_vars
# (real sqlite DB via db.init_db, a `sessions` row, a `visible_resources`
# grant, and the shared ContextVars send_attachment/skills.filesystem read).
@pytest.fixture
def real_gate(tmp_path):
    conn = db_module.init_db(str(tmp_path / "agent.db"),
                             snapshots_root=str(tmp_path / "snap"))
    now = int(time.time())
    conn.execute(
        "INSERT INTO sessions (id, user_id, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?)", ("s1", "u1", None, now, now))
    granted = tmp_path / "granted"
    granted.mkdir()
    (granted / "ok.txt").write_text("hello")
    conn.execute(
        "INSERT INTO visible_resources (session_id, path, kind, added_at) "
        "VALUES (?,?,?,?)", ("s1", str(granted), "folder", now))
    conn.commit()

    send_attachment_mod.SESSION_ID_VAR.set("s1")
    fs_skill.DB_VAR.set(conn)
    fs_skill.USER_PATTERNS_VAR.set([])
    return conn, granted


def test_default_validate_allows_real_path_inside_granted_scope(real_gate):
    _conn, granted = real_gate
    target = str(granted / "ok.txt")
    assert _default_validate(target) == target


def test_default_validate_denies_path_outside_scope(real_gate):
    assert _default_validate("/etc/passwd") is None


def test_default_validate_denies_traversal_after_realpath(real_gate):
    _conn, granted = real_gate
    traversal = str(granted) + "/../etc/passwd"
    assert _default_validate(traversal) is None
