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


import asyncio
import json
import types


def test_parse_ollama_show_trusts_only_num_ctx_never_model_info():
    # Final review Major 1: model_info.*.context_length is the model's
    # advertised CAPABILITY, not the window Ollama actually serves (num_ctx,
    # default 8192) — trusting it silently disabled compaction for local
    # chat (measured 262144 on a real box). Only a Modelfile num_ctx is a
    # real served window; model_info alone must fall through to the
    # LOCAL_CONTEXT_WINDOW tier default (None here, not 131072).
    assert mw._parse_ollama_show({"parameters": "num_ctx                        32768\nstop  <|im_end|>",
                                  "model_info": {"qwen3.context_length": 40960}}) == 32768
    assert mw._parse_ollama_show({"model_info": {"llama.context_length": 131072}}) is None
    assert mw._parse_ollama_show({"model_info": {"qwen35.context_length": 262144}}) is None
    assert mw._parse_ollama_show({"parameters": "stop x", "model_info": {"llama.context_length": 131072}}) is None
    assert mw._parse_ollama_show({"parameters": "stop x"}) is None
    assert mw._parse_ollama_show({}) is None


def test_parse_models_list_matches_id_and_known_fields():
    payload = {"data": [{"id": "gpt-x", "context_window": 200000},
                        {"id": "deepseek-chat", "context_length": 128000},
                        {"id": "other", "max_context_length": "64000"}]}
    assert mw._parse_models_list(payload, "deepseek-chat") == 128000
    assert mw._parse_models_list(payload, "gpt-x") == 200000
    assert mw._parse_models_list(payload, "other") == 64000
    assert mw._parse_models_list(payload, "missing") is None
    assert mw._parse_models_list({"data": [{"id": "m"}]}, "m") is None


class _Resp:
    def __init__(self, status, payload): self.status_code = status; self._p = payload
    def json(self): return self._p


class _Client:
    """httpx.AsyncClient stand-in recording calls."""
    calls: list = []
    routes: dict = {}
    def __init__(self, *a, **kw): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url, headers=None):
        _Client.calls.append(("GET", url, headers)); return _Client.routes.get(("GET", url), _Resp(404, {}))
    async def post(self, url, json=None, headers=None):
        _Client.calls.append(("POST", url, json)); return _Client.routes.get(("POST", url), _Resp(404, {}))


@pytest.fixture
def fake_httpx(monkeypatch):
    import httpx
    _Client.calls, _Client.routes = [], {}
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    mw._TRIED.clear()
    return _Client


def test_fetch_window_ollama_uses_api_show(fake_httpx):
    fake_httpx.routes[("POST", "http://127.0.0.1:11434/api/show")] = _Resp(200, {"parameters": "num_ctx 16384"})
    w = asyncio.new_event_loop().run_until_complete(
        mw.fetch_window("ollama", "http://127.0.0.1:11434/v1/", "qwen3:8b"))
    assert w == 16384
    assert fake_httpx.calls[0][1] == "http://127.0.0.1:11434/api/show" and fake_httpx.calls[0][2] == {"name": "qwen3:8b"}


def test_fetch_window_openai_compatible_uses_models_list_with_bearer(fake_httpx):
    fake_httpx.routes[("GET", "https://openrouter.ai/api/v1/models")] = _Resp(200, {"data": [{"id": "x/y", "context_length": 65536}]})
    w = asyncio.new_event_loop().run_until_complete(
        mw.fetch_window("other", "https://openrouter.ai/api/v1", "x/y", api_key="k"))
    assert w == 65536 and fake_httpx.calls[0][2] == {"Authorization": "Bearer k"}


def test_fetch_window_never_raises(fake_httpx, monkeypatch):
    async def boom(*a, **k): raise RuntimeError("net down")
    monkeypatch.setattr(_Client, "get", boom)
    assert asyncio.new_event_loop().run_until_complete(mw.fetch_window("other", "https://x/v1", "m")) is None


def test_ensure_fetched_stores_once_and_respects_manual(conn, fake_httpx):
    fake_httpx.routes[("GET", "https://api.x/v1/models")] = _Resp(200, {"data": [{"id": "m", "context_length": 32000}]})
    run = lambda: asyncio.new_event_loop().run_until_complete(  # noqa: E731
        mw.ensure_fetched(conn, provider_type="other", provider_url="https://api.x/v1", model_name="m", api_key=""))
    run()
    assert mw.get(conn, "cloud:m") == {**mw.get(conn, "cloud:m"), "window": 32000, "source": "fetched"}
    run(); run()
    assert len(fake_httpx.calls) == 1                     # tried once per process
    mw._TRIED.clear()
    mw.upsert(conn, "cloud:m", 20000, "manual")
    run()
    assert len(fake_httpx.calls) == 1                     # manual row → no fetch at all
    assert mw.get(conn, "cloud:m")["window"] == 20000


def test_ensure_fetched_also_respects_a_learned_row(conn, fake_httpx):
    # A learned row is evidence from a real context-limit 400 and must block
    # metadata fetching too, or a later fetched value could silently undo the
    # shrink that learn() was meant to guarantee.
    fake_httpx.routes[("GET", "https://api.x/v1/models")] = _Resp(200, {"data": [{"id": "m", "context_length": 99000}]})
    mw.upsert(conn, "cloud:m", 40000, "learned")
    asyncio.new_event_loop().run_until_complete(
        mw.ensure_fetched(conn, provider_type="other", provider_url="https://api.x/v1", model_name="m", api_key=""))
    assert len(fake_httpx.calls) == 0                     # learned row → no fetch at all
    assert mw.get(conn, "cloud:m") == {**mw.get(conn, "cloud:m"), "window": 40000, "source": "learned"}


def test_model_key_handles_colon_inside_bare_name():
    assert mw.model_key("cloud:4:qwen3:32b") == "cloud:qwen3:32b"


def test_model_key_mangles_colon_bearing_name_given_without_provider_id():
    # Nit 3 (final review): unreachable through either real caller (the UI
    # always sends "cloud:<id>:<name>"; runs send a bare name + provider_type)
    # but pin the current behaviour so a future caller can't introduce this
    # silently — "cloud:gpt:4" strips the middle segment as if it were a
    # provider id, losing "gpt".
    assert mw.model_key("cloud:gpt:4") == "cloud:4"


def test_to_int_accepts_float_valued_numbers():
    # Minor 11 (final review): a provider reporting a JSON float
    # ("context_length": 8192.0) or its string form must still parse.
    assert mw._to_int(8192.0) == 8192
    assert mw._to_int("8192.0") == 8192
    assert mw._to_int("8192") == 8192
    assert mw._to_int("not a number") is None
    assert mw._to_int(0.0) is None


def test_upsert_enforces_max_window_ceiling(conn):
    # Minor 4 (final review): no upper bound previously let a typo (or a
    # capability number mistaken for a served window) disable compaction.
    with pytest.raises(ValueError):
        mw.upsert(conn, "cloud:m", cc.MAX_CONTEXT_WINDOW + 1, "manual")
    mw.upsert(conn, "cloud:m", cc.MAX_CONTEXT_WINDOW, "manual")
    assert mw.get(conn, "cloud:m")["window"] == cc.MAX_CONTEXT_WINDOW
