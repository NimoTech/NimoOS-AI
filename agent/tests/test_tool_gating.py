from skills import tool_gating as tg


def test_current_unlocked_default_empty():
    # returns an empty set when unset, does not raise LookupError
    tg.UNLOCKED_VAR.set(set())
    assert tg.current_unlocked() == set()


def test_is_enabled_reflects_var():
    tg.UNLOCKED_VAR.set({"apps"})
    apps_ok = tg.make_is_enabled("apps")
    files_ok = tg.make_is_enabled("files")
    assert apps_ok(None, None) is True
    assert files_ok(None, None) is False


def test_is_enabled_sees_in_place_mutation():
    s = set()
    tg.UNLOCKED_VAR.set(s)
    check = tg.make_is_enabled("wiki")
    assert check(None, None) is False
    s.add("wiki")                      # in-place mutation
    assert check(None, None) is True
