import os
from agents import function_tool
from nimoos_cli import run_cli

CLI_BIN = os.environ.get("NIMOOS_CLI_PATH", "nimoos-cli")


@function_tool
async def check_services() -> str:
    """Check the health status of all NimoOS services."""
    return await run_cli([CLI_BIN, "healthcheck", "services"])


@function_tool
async def check_ports() -> str:
    """List ports currently in use on this NimoOS system."""
    return await run_cli([CLI_BIN, "healthcheck", "ports-in-use"])


@function_tool
async def get_system_logs() -> str:
    """Retrieve recent system logs from NimoOS."""
    return await run_cli([CLI_BIN, "healthcheck", "logs"])


ALL_TOOLS = [check_services, check_ports, get_system_logs]
