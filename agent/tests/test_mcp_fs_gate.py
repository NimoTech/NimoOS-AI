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
