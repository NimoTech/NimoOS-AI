"""Agent tool: install a toolbox component after in-chat confirmation.

Pattern mirrors skills/mcp_admin.py: register a confirmation with the shared
ConfirmManager, emit a `confirmation_required` event on the session's event
queue, await the user's decision, then perform the install."""
from __future__ import annotations

from agents import function_tool


async def _do_install(conn, component_id):
    from toolbox import installer
    await installer.install(conn, component_id)


@function_tool
async def install_component(component_id: str) -> str:
    """Install a CLI component from the NimoOS toolbox catalog (persists across container rebuilds).

    Args:
        component_id: catalog id, e.g. "lark-cli" or "gh".
    """
    from toolbox import installer
    from mcp_client.client import CONFIRM_MGR_VAR, EVENT_QUEUE_VAR, SESSION_ID_VAR
    import db as _db
    try:
        comp = installer._catalog_by_id(component_id)
    except installer.InstallError:
        return f"Unknown component '{component_id}'. Available: " + \
               ", ".join(c["id"] for c in installer.load_catalog())
    mgr = CONFIRM_MGR_VAR.get(); queue = EVENT_QUEUE_VAR.get(); sid = SESSION_ID_VAR.get()
    if not (mgr and queue and sid):
        return "Cannot install without a confirmation channel."
    _auto = False
    try:  # global permission policy waiver (audited); fail toward asking
        import permissions as _perm  # noqa: PLC0415
        from audit import audit as _audit  # noqa: PLC0415
        if _perm.auto_approve(_db.get_connection(),
                              f"toolbox_install:{component_id}"):
            _audit("toolbox_install", session_id=sid, component=component_id,
                   decision="auto_approved_by_policy")
            _auto = True
    except Exception:  # noqa: BLE001
        _auto = False
    if not _auto:
        confirm_id = mgr.register(sid, f"toolbox_install:{component_id}",
                                  f"Install {comp['name']} v{comp['version']}", component_id)
        await queue.put({"type": "confirmation_required", "confirm_id": confirm_id,
                         "kind": "toolbox_install", "title": f"Install {comp['name']} v{comp['version']}",
                         "detail": comp["description"]})
        if not await mgr.wait(confirm_id):
            return "The user denied the toolbox install."
    try:
        await _do_install(_db.get_connection(), component_id)
    except Exception as e:
        return f"Install failed: {e}"
    return f"{comp['name']} v{comp['version']} installed. It is on PATH for sandbox commands immediately."


ALL_TOOLS = [install_component]
