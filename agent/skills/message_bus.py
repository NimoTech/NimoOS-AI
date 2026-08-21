import json
import os
from contextvars import ContextVar
from agents import function_tool
from nimoos_cli import run_cli, validate_action_type

CLI_BIN = os.environ.get("NIMOOS_CLI_PATH", "nimoos-cli")

SESSION_ID_VAR: ContextVar[str] = ContextVar("session_id")
EVENT_QUEUE_VAR: ContextVar = ContextVar("event_queue")
CONFIRM_MGR_VAR: ContextVar = ContextVar("confirm_mgr")


@function_tool
async def list_event_types() -> str:
    """List all available event types on the NimoOS MessageBus."""
    return await run_cli([CLI_BIN, "message-bus", "list", "event-types"])


@function_tool
async def list_action_types() -> str:
    """List all available action types on the NimoOS MessageBus."""
    return await run_cli([CLI_BIN, "message-bus", "list", "action-types"])


@function_tool
async def trigger_action(action_type: str, data: str = "{}") -> str:
    """Trigger a MessageBus action. action_type: the action type name. data: JSON payload string."""
    try:
        validate_action_type(action_type)
    except ValueError as e:
        return f"Error: {e}"

    try:
        json.loads(data)
    except json.JSONDecodeError as e:
        return f"Error: data is not valid JSON: {e}"

    session_id = SESSION_ID_VAR.get()
    queue = EVENT_QUEUE_VAR.get()
    mgr = CONFIRM_MGR_VAR.get()
    description = f"Trigger MessageBus action '{action_type}' with data: {data}"
    command = f"nimoos-cli message-bus trigger action --type {action_type} --data {data}"

    import permissions as _perm  # noqa: PLC0415
    if _perm.policy_waives("trigger_action", session_id=session_id,
                           command=command):
        return await run_cli([CLI_BIN, "message-bus", "trigger", "action",
                              "--type", action_type, "--data", data])

    confirm_id = mgr.register(session_id, "trigger_action", description, command)
    await queue.put({
        "type": "confirmation_required",
        "confirm_id": confirm_id,
        "action": "trigger_action",
        "description": description,
        "command": command,
    })
    confirmed = await mgr.wait(confirm_id)
    if not confirmed:
        return "Operation cancelled by user."
    return await run_cli([CLI_BIN, "message-bus", "trigger", "action",
                          "--type", action_type, "--data", data])


ALL_TOOLS = [list_event_types, list_action_types, trigger_action]
