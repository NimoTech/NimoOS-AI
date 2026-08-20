from skills import mcp_gating as mg
from skills import tool_gating as tg


def test_gate_key_uses_server_id_not_slug():
    """The persisted unlocked set must store the id: if it stored the slug,
    a user renaming a server (or a new server taking over a freed slug)
    would inherit someone else's unlocked state."""
    assert mg.gate_key(7) == "mcp#7"


def test_resolve_handle_maps_slug_to_id():
    mg.MCP_HANDLES_VAR.set({"github": 7, "github_2": 9})
    assert mg.resolve_handle("mcp:github") == 7
    assert mg.resolve_handle("mcp:github_2") == 9
    assert mg.resolve_handle("mcp:nope") is None


def test_expand_unknown_mcp_handle_lists_valid_ones():
    """Self-correcting mode: an unresolvable handle returns the list of valid
    handles, consistent with tool_gating.py:82-84."""
    mg.MCP_HANDLES_VAR.set({"github": 7})
    tg.UNLOCKED_VAR.set(set())
    out = tg.expand_categories(["mcp:nope"])
    assert "github" in out and "mcp:nope" in out


def test_expand_mcp_handle_unlocks_only_that_server():
    mg.MCP_HANDLES_VAR.set({"github": 7, "notion": 9})
    tg.UNLOCKED_VAR.set(set())
    tg.expand_categories(["mcp:github"])
    unlocked = tg.current_unlocked()
    assert "mcp#7" in unlocked
    assert "mcp#9" not in unlocked, "expanding one server must not unlock the others"


def test_expand_mcp_category_alone_does_not_unlock_any_server():
    """L1 only provides the catalogue, it never loads any schema — that is
    the core of the three-level progressive disclosure."""
    mg.MCP_HANDLES_VAR.set({"github": 7})
    tg.UNLOCKED_VAR.set(set())
    tg.expand_categories(["mcp"])
    assert "mcp#7" not in tg.current_unlocked()
