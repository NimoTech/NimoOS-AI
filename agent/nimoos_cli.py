import asyncio
import os
import re
import subprocess
import tempfile
from functools import partial

CLI_BIN = os.environ.get("NIMOOS_CLI_PATH", "nimoos-cli")
CLI_TIMEOUT = 30  # seconds


class CLIError(Exception):
    pass


def validate_id(value: str) -> str:
    if not re.fullmatch(r"[\w-]+", value):
        raise ValueError(f"Invalid id format: {value!r}")
    return value


def validate_query(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9\s_\-\.]+", value):
        raise ValueError(f"Invalid query format: {value!r}")
    return value


def validate_action_type(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_\-\.]*", value):
        raise ValueError(f"Invalid action type format: {value!r}")
    return value


def _run_sync(cmd: list[str]) -> str:
    try:
        result = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT,
        )
        if result.returncode != 0:
            return result.stderr or f"Command failed with exit code {result.returncode}"
        return result.stdout
    except subprocess.TimeoutExpired:
        return f"Command timed out after {CLI_TIMEOUT}s: {' '.join(cmd)}"


async def run_cli(cmd: list[str]) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(_run_sync, cmd))


async def run_cli_with_yaml(subcommand: list[str], yaml_content: str) -> str:
    """Write yaml to a temp file, pass its path to CLI, always delete the file."""
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=True) as f:
        f.write(yaml_content)
        f.flush()
        return await run_cli(subcommand + [f.name])
