import os
from agents import function_tool
from nimoos_cli import run_cli

CLI_BIN = os.environ.get("NIMOOS_CLI_PATH", "nimoos-cli")


@function_tool
async def list_storage() -> str:
    """List all storage devices and mount points on this NimoOS NAS."""
    return await run_cli([CLI_BIN, "local-storage", "list"])


@function_tool
async def list_merges() -> str:
    """List MergerFS merge volume configurations."""
    return await run_cli([CLI_BIN, "local-storage", "list", "merges"])


ALL_TOOLS = [list_storage, list_merges]
