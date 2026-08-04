"""Boundary guard: this project talks to the official MCP SDK directly.

Routing the MCP protocol layer through agents.mcp would put a SECOND parallel MCP
client stack in the same process: two capability declarations (one declaring
elicitation and one not — so a remote server's behaviour would vary by which code
path reached it, a bug that is near-impossible to track down), two connection
pools, two schema caches, and a renewed dependency on the private class
_MCPServerWithClientSession.

This boundary stays valid even after upstream supports mcp 2.x — the test forbids
US IMPORTING IT, which is independent of what protocol upstream speaks. Do not
delete it when openai-agents catches up.
"""
import pathlib
import sys

FORBIDDEN = (
    "agents.mcp.server",
    "MCPServerStreamableHttp",
    "MCPServerSse",
    "MCPServerStdio",
    "_MCPServerWithClientSession",
)

AGENT_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_no_agents_mcp_transport_symbols_in_source():
    offenders = []
    for path in AGENT_ROOT.rglob("*.py"):
        if "tests" in path.relative_to(AGENT_ROOT).parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for symbol in FORBIDDEN:
            if symbol in text:
                offenders.append(f"{path.relative_to(AGENT_ROOT)}: {symbol}")
    assert not offenders, "agents.mcp transport symbols leaked back in:\n" + "\n".join(offenders)


def test_importing_our_client_never_loads_agents_mcp_server():
    """Stronger than the grep: catches an INDIRECT import too."""
    sys.modules.pop("agents.mcp.server", None)
    import agent  # noqa: F401
    import mcp_client.client  # noqa: F401

    assert "agents.mcp.server" not in sys.modules
