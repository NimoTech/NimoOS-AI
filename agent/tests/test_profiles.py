import profiles


def test_photos_profile_tools_exact():
    p = profiles.PROFILES["photos"]
    names = sorted(t.name for t in p.tools)
    assert names == ["add_to_album", "create_album", "get_album_summary",
                     "list_albums", "rename_album", "search_photos"]
    assert p.compose_resources is False


def test_general_profile_is_passthrough():
    p = profiles.PROFILES["general"]
    assert p.tools is None
    assert p.prompt is None
    assert p.compose_resources is True


def test_get_profile_fallbacks():
    assert profiles.get_profile(None) is profiles.PROFILES["general"]
    assert profiles.get_profile("") is profiles.PROFILES["general"]
    assert profiles.get_profile("bogus") is profiles.PROFILES["general"]
    assert profiles.get_profile("photos") is profiles.PROFILES["photos"]


def test_photos_prompt_scopes_capabilities():
    p = profiles.PROFILES["photos"].prompt
    assert "search_photos" in p
    assert "album" in p.lower()


def test_photos_prompt_rejects_filesystem():
    # The prompt's core safety constraint: refuse file operations and point
    # the user to the main AI app. Guard the wording against rewrites.
    p = profiles.PROFILES["photos"].prompt
    assert "no filesystem" in p
    assert "main Nimo AI app" in p


def test_photos_prompt_album_draft_workflow():
    # When the UI hands over a freshly created album, the agent must fill
    # THAT album (given album_id) instead of creating a duplicate.
    p = profiles.PROFILES["photos"].prompt
    assert "Target album" in p
    assert "add_to_album" in p
