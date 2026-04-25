import pytest
from unittest.mock import patch, MagicMock
import subprocess
from nimoos_cli import run_cli, validate_id, validate_query, CLIError

def test_validate_id_accepts_alphanumeric():
    assert validate_id("42") == "42"
    assert validate_id("abc-123_xyz") == "abc-123_xyz"

def test_validate_id_rejects_injection():
    with pytest.raises(ValueError):
        validate_id("42; rm -rf /")
    with pytest.raises(ValueError):
        validate_id("42 | cat /etc/passwd")

def test_validate_query_accepts_safe():
    assert validate_query("plex media server") == "plex media server"

def test_validate_query_rejects_special_chars():
    with pytest.raises(ValueError):
        validate_query("plex; rm -rf /")

@pytest.mark.asyncio
async def test_run_cli_returns_stdout():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "app list output"
    mock_result.stderr = ""
    with patch("nimoos_cli.subprocess.run", return_value=mock_result) as mock_run:
        result = await run_cli(["nimoos-cli", "app-management", "list", "apps"])
        # Verify shell=False (no shell kwarg or shell=False)
        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs.get("shell", False) is False
        assert result == "app list output"

@pytest.mark.asyncio
async def test_run_cli_returns_stderr_on_failure():
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "app not found"
    with patch("nimoos_cli.subprocess.run", return_value=mock_result):
        result = await run_cli(["nimoos-cli", "app-management", "start", "999"])
        assert "app not found" in result

@pytest.mark.asyncio
async def test_run_cli_handles_timeout():
    with patch("nimoos_cli.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=[], timeout=30)):
        result = await run_cli(["nimoos-cli", "healthcheck", "services"])
        assert "timed out" in result.lower()
