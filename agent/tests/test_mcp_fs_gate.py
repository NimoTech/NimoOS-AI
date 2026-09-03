import os
import pytest
from mcp_server.fs_gate import mcp_resolve_read_path, McpPathDenied


def test_allows_path_inside_root(tmp_path):
    root = str(tmp_path)
    f = tmp_path / "a.pdf"
    f.write_text("x")
    assert mcp_resolve_read_path(str(f), root=root) == os.path.realpath(str(f))


def test_denies_path_outside_root(tmp_path):
    root = str(tmp_path / "DATA")
    os.makedirs(root)
    with pytest.raises(McpPathDenied):
        mcp_resolve_read_path("/etc/passwd", root=root)


def test_denies_traversal_escape(tmp_path):
    root = str(tmp_path / "DATA")
    os.makedirs(root)
    with pytest.raises(McpPathDenied):
        mcp_resolve_read_path(os.path.join(root, "..", "secret.txt"), root=root)


def test_denies_system_data(tmp_path):
    root = str(tmp_path / "DATA")
    os.makedirs(os.path.join(root, ".system_data"))
    with pytest.raises(McpPathDenied):
        mcp_resolve_read_path(os.path.join(root, ".system_data", "photos.db"), root=root)


def test_denies_symlink_escape(tmp_path):
    root = str(tmp_path / "DATA")
    os.makedirs(root)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = os.path.join(root, "link.txt")
    os.symlink(str(outside), link)
    with pytest.raises(McpPathDenied):
        mcp_resolve_read_path(link, root=root)


def test_denies_empty(tmp_path):
    with pytest.raises(McpPathDenied):
        mcp_resolve_read_path("", root=str(tmp_path))


# --- hard blacklist + per-user notes isolation (audit 2026-09-03, AI P1) ---

def test_denies_hard_blacklisted_credentials_inside_root(tmp_path):
    root = str(tmp_path / "DATA")
    os.makedirs(os.path.join(root, "home", ".ssh"))
    key = os.path.join(root, "home", ".ssh", "id_rsa")
    open(key, "w").write("k")
    with pytest.raises(McpPathDenied):
        mcp_resolve_read_path(key, root=root)


def test_denies_pem_anywhere_inside_root(tmp_path):
    root = str(tmp_path / "DATA")
    os.makedirs(os.path.join(root, "certs"))
    pem = os.path.join(root, "certs", "server.pem")
    open(pem, "w").write("k")
    with pytest.raises(McpPathDenied):
        mcp_resolve_read_path(pem, root=root)


def test_denies_other_users_notes(tmp_path):
    root = str(tmp_path / "DATA")
    notes = os.path.join(root, "Notes")
    os.makedirs(os.path.join(notes, "2"))
    other = os.path.join(notes, "2", "secret.md")
    open(other, "w").write("theirs")
    with pytest.raises(McpPathDenied):
        mcp_resolve_read_path(other, root=root, user_id="1", notes_root=notes)


def test_allows_own_notes(tmp_path):
    root = str(tmp_path / "DATA")
    notes = os.path.join(root, "Notes")
    os.makedirs(os.path.join(notes, "1"))
    mine = os.path.join(notes, "1", "mine.md")
    open(mine, "w").write("mine")
    assert mcp_resolve_read_path(mine, root=root, user_id="1", notes_root=notes) == os.path.realpath(mine)


def test_denies_notes_when_caller_identity_unknown(tmp_path):
    root = str(tmp_path / "DATA")
    notes = os.path.join(root, "Notes")
    os.makedirs(os.path.join(notes, "1"))
    p = os.path.join(notes, "1", "x.md")
    open(p, "w").write("x")
    with pytest.raises(McpPathDenied):
        mcp_resolve_read_path(p, root=root, user_id=None, notes_root=notes)


def test_notes_root_itself_is_not_readable(tmp_path):
    root = str(tmp_path / "DATA")
    notes = os.path.join(root, "Notes")
    os.makedirs(notes)
    with pytest.raises(McpPathDenied):
        mcp_resolve_read_path(notes, root=root, user_id="1", notes_root=notes)


def test_regular_document_unaffected_by_notes_rule(tmp_path):
    root = str(tmp_path / "DATA")
    os.makedirs(os.path.join(root, "Documents"))
    doc = os.path.join(root, "Documents", "report.pdf")
    open(doc, "w").write("x")
    assert mcp_resolve_read_path(doc, root=root, user_id="1",
                                 notes_root=os.path.join(root, "Notes")) == os.path.realpath(doc)
