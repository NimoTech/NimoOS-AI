import json
import os
from contextvars import ContextVar
from agents import function_tool
from nimoos_cli import run_cli, run_cli_with_yaml, validate_id, validate_query

CLI_BIN = os.environ.get("NIMOOS_CLI_PATH", "nimoos-cli")

# Injected by agent loop before each run
SESSION_ID_VAR: ContextVar[str] = ContextVar("session_id")
EVENT_QUEUE_VAR: ContextVar = ContextVar("event_queue")
CONFIRM_MGR_VAR: ContextVar = ContextVar("confirm_mgr")


def _queue():
    return EVENT_QUEUE_VAR.get()

def _mgr():
    return CONFIRM_MGR_VAR.get()

def _session_id():
    return SESSION_ID_VAR.get()


@function_tool
async def list_apps() -> str:
    """List all installed applications on this NimoOS NAS."""
    return await run_cli([CLI_BIN, "app-management", "list", "apps"])


@function_tool
async def search_apps(query: str) -> str:
    """Search for applications in the AppStore. query: search term."""
    try:
        query = validate_query(query)
    except ValueError as e:
        return f"Error: {e}"
    return await run_cli([CLI_BIN, "app-management", "search", query])


@function_tool
async def get_app_logs(app_id: str) -> str:
    """Get recent logs for an application. app_id: the application ID."""
    try:
        app_id = validate_id(app_id)
    except ValueError as e:
        return f"Error: {e}"
    return await run_cli([CLI_BIN, "app-management", "logs", app_id])


@function_tool
async def show_app(app_id: str) -> str:
    """Show details of an installed application. app_id: the application ID."""
    try:
        app_id = validate_id(app_id)
    except ValueError as e:
        return f"Error: {e}"
    return await run_cli([CLI_BIN, "app-management", "show", "local", app_id])


async def _write_op(action: str, description: str, command_preview: str, cli_cmd: list[str]) -> str:
    """Common pattern for write operations: emit confirm event, wait, execute."""
    session_id = _session_id()
    confirm_id = _mgr().register(session_id, action, description, command_preview)
    await _queue().put({
        "type": "confirmation_required",
        "confirm_id": confirm_id,
        "action": action,
        "description": description,
        "command": command_preview,
    })
    confirmed = await _mgr().wait(confirm_id)
    if not confirmed:
        return "Operation cancelled by user."
    return await run_cli(cli_cmd)


@function_tool
async def install_app(yaml_content: str) -> str:
    """Install an application from a YAML definition. yaml_content: the app YAML."""
    session_id = _session_id()
    description = "Install a new application from YAML definition. Confirm to proceed."
    command = "nimoos-cli app-management install <yaml>"
    confirm_id = _mgr().register(session_id, "install_app", description, command)
    await _queue().put({
        "type": "confirmation_required",
        "confirm_id": confirm_id,
        "action": "install_app",
        "description": description,
        "command": command,
    })
    confirmed = await _mgr().wait(confirm_id)
    if not confirmed:
        return "Operation cancelled by user."
    return await run_cli_with_yaml([CLI_BIN, "app-management", "install"], yaml_content)


@function_tool
async def start_app(app_id: str) -> str:
    """Start a stopped application. app_id: the application ID."""
    try:
        app_id = validate_id(app_id)
    except ValueError as e:
        return f"Error: {e}"
    return await _write_op("start_app", f"Start application {app_id}?",
                           f"nimoos-cli app-management start {app_id}",
                           [CLI_BIN, "app-management", "start", app_id])


@function_tool
async def stop_app(app_id: str) -> str:
    """Stop a running application. app_id: the application ID."""
    try:
        app_id = validate_id(app_id)
    except ValueError as e:
        return f"Error: {e}"
    return await _write_op("stop_app", f"Stop application {app_id}?",
                           f"nimoos-cli app-management stop {app_id}",
                           [CLI_BIN, "app-management", "stop", app_id])


@function_tool
async def restart_app(app_id: str) -> str:
    """Restart an application. app_id: the application ID."""
    try:
        app_id = validate_id(app_id)
    except ValueError as e:
        return f"Error: {e}"
    return await _write_op("restart_app", f"Restart application {app_id}?",
                           f"nimoos-cli app-management restart {app_id}",
                           [CLI_BIN, "app-management", "restart", app_id])


@function_tool
async def uninstall_app(app_id: str) -> str:
    """Uninstall an application. app_id: the application ID."""
    try:
        app_id = validate_id(app_id)
    except ValueError as e:
        return f"Error: {e}"
    return await _write_op("uninstall_app", f"Uninstall application {app_id}? This cannot be undone.",
                           f"nimoos-cli app-management uninstall {app_id}",
                           [CLI_BIN, "app-management", "uninstall", app_id])


@function_tool
async def update_app(app_id: str) -> str:
    """Update an application to the latest version. app_id: the application ID."""
    try:
        app_id = validate_id(app_id)
    except ValueError as e:
        return f"Error: {e}"
    return await _write_op("update_app", f"Update application {app_id} to latest version?",
                           f"nimoos-cli app-management update {app_id}",
                           [CLI_BIN, "app-management", "update", app_id])


ALL_TOOLS = [
    list_apps, search_apps, get_app_logs, show_app,
    install_app, start_app, stop_app, restart_app, uninstall_app, update_app,
]
