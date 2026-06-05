"""Agent profiles: per-entry-point tool sets and system prompts.

A profile is selected at session creation time (sessions.agent_type) and
enforced server-side on every run. The tool whitelist lives HERE, never in
the request body — clients can only pick a registered profile name, so a
forged request can at most select 'general' (today's behavior).
"""
from dataclasses import dataclass

from skills.photos import ALL_TOOLS as PHOTOS_TOOLS


@dataclass(frozen=True)
class Profile:
    # None = full ALL_TOOLS pipeline incl. the dynamic read_attachment append
    # (current behavior). A tuple pins the tool list with no dynamic additions.
    tools: tuple | None
    # None = default SYSTEM_PROMPT pipeline. A string replaces the base prompt.
    prompt: str | None
    # False = skip the wiki context block and the visible-resources block when
    # composing the system prompt (profiles with no filesystem access).
    compose_resources: bool = True


PHOTOS_SYSTEM_PROMPT = """You are Nimo, the photo assistant inside the NimoOS Photos app.

You work exclusively with the user's photo library through your photo tools:
- search_photos: semantic search over the indexed library (CLIP). This is the
  only way to locate photos — the index covers the whole library regardless of
  where files physically live.
- create_album / add_to_album: organize photos into albums. Albums are
  database records in the Photos service; they never move, rename, or modify
  the underlying files.

Behavior rules:
- You have no filesystem, shell, app-management, or system tools. If the user
  asks you to delete/move/rename files, install apps, or manage the NAS,
  explain that this is outside the Photos assistant's scope and point them to
  the main Nimo AI app for those tasks. Never attempt workarounds.
- Photo search and album organization are safe operations — act immediately,
  no confirmation needed.
- Keep replies short and conversational; the Photos chat UI is compact.
- Match the user's language."""


PROFILES = {
    "general": Profile(tools=None, prompt=None, compose_resources=True),
    "photos": Profile(tools=tuple(PHOTOS_TOOLS),
                      prompt=PHOTOS_SYSTEM_PROMPT,
                      compose_resources=False),
}


# Guard against a future refactor silently emptying the photos tool set —
# that would degrade to "prompt but no tools" and be hard to notice.
assert len(PROFILES["photos"].tools) > 0, "photos profile has no tools"


def get_profile(agent_type: str | None) -> Profile:
    """Resolve an agent_type to its Profile.

    Unknown/legacy/empty values fall back to 'general' — pre-profile session
    rows behave exactly as before.
    """
    return PROFILES.get(agent_type or "general", PROFILES["general"])
