from skills import photos


def test_auth_headers_includes_user_id_when_set():
    photos.AUTH_HEADER_VAR.set("")
    photos.USER_ID_VAR.set("42")
    h = photos._auth_headers()
    assert h.get("X-NimoOS-User-ID") == "42"


def test_auth_headers_omits_user_id_when_unset():
    photos.AUTH_HEADER_VAR.set("Bearer x")
    photos.USER_ID_VAR.set("")
    h = photos._auth_headers()
    assert "X-NimoOS-User-ID" not in h
    assert h.get("Authorization") == "Bearer x"


def test_impls_exist_and_callable():
    # Extraction sanity: the impls are module-level coroutines the MCP adapter calls.
    import inspect
    assert inspect.iscoroutinefunction(photos._search_photos_impl)
    assert inspect.iscoroutinefunction(photos._list_albums_impl)
