import skills.photos as photos_skills


def test_auth_headers_empty_by_default():
    # No leaked state between runs: default is no Authorization header at all.
    photos_skills.AUTH_HEADER_VAR.set("")
    assert photos_skills._auth_headers() == {}


def test_auth_headers_carries_bearer_token():
    photos_skills.AUTH_HEADER_VAR.set("Bearer abc.def.ghi")
    try:
        assert photos_skills._auth_headers() == {
            "Authorization": "Bearer abc.def.ghi"}
    finally:
        photos_skills.AUTH_HEADER_VAR.set("")
