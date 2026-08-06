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
import subprocess
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
    """Stronger than the grep: catches an INDIRECT import too.

    Runs in a FRESH interpreter on purpose. In-process this assertion would be
    vacuous during a full-suite run: pytest has already imported these modules
    for earlier test files, so the imports here would be sys.modules cache hits
    that execute nothing — and an indirect pull-in would have happened back then,
    before this test could observe it.
    """
    code = (
        "import sys\n"
        "import agent, mcp_client.client\n"
        "agents_mods = [m for m in sys.modules if m.startswith('agents')]\n"
        "assert 'agents.mcp.server' not in sys.modules, "
        "f'agents.mcp.server found in sys.modules: {agents_mods}'\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(AGENT_ROOT),
        capture_output=True,
        text=True
    )
    assert proc.returncode == 0, f"Import check failed:\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
