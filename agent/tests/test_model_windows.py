import pytest

import context_compaction as cc
import model_windows as mw
from db import init_db


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "mw.db"))
    c.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) VALUES('s1','u1',0,0)"); c.commit()
    return c


def test_model_key_normalises_selectors_and_tiers():
    assert mw.model_key("Qwen3:32B", "ollama") == "local:qwen3:32b"
    assert mw.model_key("local:qwen3:32b") == "local:qwen3:32b"
    assert mw.model_key("cloud:4:deepseek-v4-flash-ga-260731") == "cloud:deepseek-v4-flash-ga-260731"
    assert mw.model_key("deepseek-v4-flash-ga-260731", "other") == "cloud:deepseek-v4-flash-ga-260731"
    assert mw.model_key("  GPT-5 ", "openai") == "cloud:gpt-5"


def test_upsert_precedence_manual_wins_and_learned_only_decreases(conn):
    k = "cloud:m"
    assert mw.get(conn, k) is None
    mw.upsert(conn, k, 100_000, "learned")
    mw.upsert(conn, k, 120_000, "learned")           # learned never increases
    assert mw.get(conn, k)["window"] == 100_000
    mw.upsert(conn, k, 90_000, "learned")
    assert mw.get(conn, k)["window"] == 90_000
    mw.upsert(conn, k, 200_000, "fetched")           # fetched replaces learned
    assert mw.get(conn, k) == {**mw.get(conn, k), "window": 200_000, "source": "fetched"}
    mw.upsert(conn, k, 64_000, "manual")             # manual replaces anything
    mw.upsert(conn, k, 300_000, "fetched")           # ...and is never overwritten
    mw.upsert(conn, k, 10_000, "learned")
    assert mw.get(conn, k)["window"] == 64_000 and mw.get(conn, k)["source"] == "manual"
    mw.delete_manual(conn, k)
    assert mw.get(conn, k) is None


def test_upsert_enforces_min_window(conn):
    with pytest.raises(ValueError):
        mw.upsert(conn, "cloud:m", cc.MIN_CONTEXT_WINDOW - 1, "manual")
    with pytest.raises(ValueError):
        mw.upsert(conn, "cloud:m", 100_000, "bogus")


def test_resolve_window_precedence_chain(conn):
    # default tiers
    assert cc.resolve_window(conn, "u1", "m", "other") == cc.CLOUD_CONTEXT_WINDOW
    assert cc.resolve_window(conn, "u1", "m", "ollama") == cc.LOCAL_CONTEXT_WINDOW
    assert cc.resolve_window_with_source(conn, "u1", "local:m") == (cc.LOCAL_CONTEXT_WINDOW, "default")
    # learned < fetched < manual
    mw.upsert(conn, "cloud:m", 100_000, "learned")
    assert cc.resolve_window_with_source(conn, "u1", "m", "other") == (100_000, "learned")
    mw.upsert(conn, "cloud:m", 120_000, "fetched")
    assert cc.resolve_window_with_source(conn, "u1", "cloud:4:m") == (120_000, "fetched")
    mw.upsert(conn, "cloud:m", 64_000, "manual")
    assert cc.resolve_window_with_source(conn, "u1", "m", "other") == (64_000, "manual")
    # user global setting beats everything
    import memory_store
    memory_store.set_context_window(conn, "u1", 50_000) if hasattr(memory_store, "set_context_window") else conn.execute(
        "INSERT INTO user_settings(user_id,key,value,updated_at) VALUES('u1','context_window','50000',0)")
    conn.commit()
    assert cc.resolve_window_with_source(conn, "u1", "m", "other") == (50_000, "user")
    assert cc.resolve_window(conn, "u1", "m", "other") == 50_000


def test_compute_usage_reports_window_source(conn):
    mw.upsert(conn, "cloud:m", 64_000, "manual")
    out = cc.compute_usage(conn, session_id="s1", user_id="u1", model="cloud:4:m")
    assert out["window"] == 64_000 and out["window_source"] == "manual"
    out2 = cc.compute_usage(conn, session_id="s1", user_id="u1", model="local:x")
    assert out2["window"] == cc.LOCAL_CONTEXT_WINDOW and out2["window_source"] == "default"
