"""web/settings.py — global web-tools config in user_settings."""
from __future__ import annotations

import db as db_module
from web import settings as web_settings


def _conn():
    return db_module.init_db(":memory:")


def test_load_defaults_when_absent():
    cfg = web_settings.load(_conn())
    assert cfg == {"backend": "", "api_key": "", "base_url": "", "enabled": False}


def test_save_then_load_roundtrip():
    conn = _conn()
    web_settings.save(conn, backend="tavily", api_key="tvly-abc",
                      base_url="", enabled=True)
    cfg = web_settings.load(conn)
    assert cfg["backend"] == "tavily"
    assert cfg["api_key"] == "tvly-abc"
    assert cfg["enabled"] is True


def test_public_view_masks_the_key():
    cfg = {"backend": "tavily", "api_key": "tvly-abc",
           "base_url": "", "enabled": True}
    view = web_settings.public_view(cfg)
    assert view == {"backend": "tavily", "base_url": "",
                    "enabled": True, "has_key": True}
    assert "api_key" not in view


def test_corrupt_json_degrades_to_defaults():
    conn = _conn()
    conn.execute(
        "INSERT INTO user_settings(user_id, key, value, updated_at) "
        "VALUES('__global__', 'web_search', 'not json', 0)")
    conn.commit()
    assert web_settings.load(conn)["backend"] == ""


def test_is_configured_requires_key_for_tavily():
    base = {"backend": "tavily", "api_key": "", "base_url": "", "enabled": True}
    assert web_settings.is_configured(base) is False
    assert web_settings.is_configured({**base, "api_key": "k"}) is True


def test_is_configured_requires_base_url_for_searxng():
    base = {"backend": "searxng", "api_key": "", "base_url": "", "enabled": True}
    assert web_settings.is_configured(base) is False
    assert web_settings.is_configured(
        {**base, "base_url": "http://searx.lan:8080"}) is True


def test_is_configured_false_when_disabled():
    cfg = {"backend": "tavily", "api_key": "k", "base_url": "", "enabled": False}
    assert web_settings.is_configured(cfg) is False


def test_preapproved_hosts_is_the_enabled_backend_only():
    assert web_settings.preapproved_hosts(
        {"backend": "tavily", "api_key": "k", "base_url": "", "enabled": True}
    ) == {"api.tavily.com"}
    assert web_settings.preapproved_hosts(
        {"backend": "brave", "api_key": "k", "base_url": "", "enabled": True}
    ) == {"api.search.brave.com"}
    assert web_settings.preapproved_hosts(
        {"backend": "searxng", "api_key": "", "enabled": True,
         "base_url": "http://Searx.LAN:8080/"}
    ) == {"searx.lan"}


def test_preapproved_hosts_empty_when_not_configured():
    assert web_settings.preapproved_hosts(
        {"backend": "tavily", "api_key": "", "base_url": "", "enabled": True}
    ) == set()
    assert web_settings.preapproved_hosts(
        {"backend": "", "api_key": "", "base_url": "", "enabled": False}
    ) == set()
