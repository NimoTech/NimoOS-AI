"""Regression test for the MCP stdio exec bypass (2026-07-16 review): a
registered stdio MCP server spawns command+args directly in the netns
executor, bypassing the shell guard. The launch command must be deny-by-default.
"""
import pytest

from mcp_client.client import _assert_stdio_command_allowed, McpCommandNotAllowed


class TestAllowed:
    @pytest.mark.parametrize("cmd", [
        "npx", "uvx", "uv", "node", "python", "python3", "deno", "bunx",
        "/usr/bin/npx", "/usr/local/bin/uvx", "/usr/bin/python3",
    ])
    def test_launchers_pass(self, cmd):
        _assert_stdio_command_allowed(cmd)  # must not raise


class TestBlocked:
    @pytest.mark.parametrize("cmd", [
        "bash", "sh", "zsh", "/bin/bash", "/usr/bin/env", "/tmp/evil",
        "./evil", "~/mcp/server", "curl", "rm",
        "/usr/local/bin/my-mcp-server", "/opt/vendor/mcp-server", "",
    ])
    def test_shells_and_offlist_rejected(self, cmd):
        with pytest.raises(McpCommandNotAllowed):
            _assert_stdio_command_allowed(cmd)
