import context_compaction as cc
from db import init_db


def test_estimate_ascii_and_cjk_bounds():
    # 100 ASCII chars ≈ 25 tokens * 1.15 ≈ 28-29
    n = cc.estimate_tokens("a" * 100)
    assert 25 <= n <= 35
    # 100 CJK chars ≈ 100 tokens * 1.15 ≈ 115
    z = cc.estimate_tokens("中" * 100)
    assert 110 <= z <= 125
    # CJK weighs far more than equal-length ASCII
    assert z > n * 3


def test_estimate_messages_sums_role_and_content():
    msgs = [{"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"}]
    total = cc.estimate_messages_tokens(msgs)
    assert total >= cc.estimate_tokens("user: hello") - 1


def test_estimate_handles_nonstring_content():
    # multimodal content (list of blocks) must not crash
    msgs = [{"role": "user",
             "content": [{"type": "input_text", "text": "check logs"}]}]
    assert cc.estimate_messages_tokens(msgs) > 0


def test_resolve_window_user_override_wins(tmp_path):
    conn = init_db(str(tmp_path / "m.db"))
    conn.execute("INSERT INTO user_settings(user_id,key,value,updated_at) "
                 "VALUES('u1','context_window','40000',0)"); conn.commit()
    assert cc.resolve_window(conn, "u1", "gpt-4o") == 40000
    conn.close()


def test_resolve_window_tier_defaults(tmp_path):
    # 2026-08-24 tier defaults: cloud 256k, local (Ollama) 8k. The old
    # per-family map is gone — new models no longer fall onto stale guesses.
    conn = init_db(str(tmp_path / "m.db"))
    # cloud: bare names (chat runs) and full selector keys (usage endpoint)
    assert cc.resolve_window(conn, "u1", "gpt-4o-mini") == cc.CLOUD_CONTEXT_WINDOW
    assert cc.resolve_window(conn, "u1", "deepseek-v4-flash-260425") == cc.CLOUD_CONTEXT_WINDOW
    assert cc.resolve_window(conn, "u1", "cloud:4:deepseek-v4-flash-260425") == cc.CLOUD_CONTEXT_WINDOW
    # local: provider_type from chat runs, "local:" key from the endpoint
    assert cc.resolve_window(conn, "u1", "qwen3:8b", "ollama") == cc.LOCAL_CONTEXT_WINDOW
    assert cc.resolve_window(conn, "u1", "local:qwen3:8b") == cc.LOCAL_CONTEXT_WINDOW
    # no signal at all → cloud
    assert cc.resolve_window(conn, "u1", "") == cc.CLOUD_CONTEXT_WINDOW
    conn.close()


def test_min_context_window_floor_constant():
    # The floor is enforced at the settings PUT (see
    # test_main_user_memory.py::test_settings_context_window_floor), not at
    # read time — compaction tests rely on tiny windows as a deterministic
    # trigger lever. Pin the constant so the API error message stays sane.
    assert 0 < cc.MIN_CONTEXT_WINDOW <= cc.LOCAL_CONTEXT_WINDOW


def test_default_window_is_modern():
    # 8192 was an absurd fallback for 2026-era cloud models: system prompt +
    # one MCP server's tool schemas alone exceed 70% of it, so unknown models
    # entered a truncate-every-turn death spiral.
    assert cc.DEFAULT_CONTEXT_WINDOW >= 32768
    assert cc.DEFAULT_CONTEXT_WINDOW == cc.CLOUD_CONTEXT_WINDOW


class _FakeTool:
    def __init__(self, name, is_enabled=True):
        self.name = name
        self.description = "d" * 40
        self.params_json_schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        if is_enabled is not None:
            self.is_enabled = is_enabled


def test_estimate_tools_skips_disabled():
    # Locked gated categories are NOT in the request — counting them inflated
    # a fresh session's overhead by ~8.5k and put local 8k-window sessions
    # permanently over budget.
    on = _FakeTool("a")
    off = _FakeTool("b", is_enabled=False)
    cb_on = _FakeTool("c", is_enabled=lambda ctx, agent: True)
    cb_off = _FakeTool("d", is_enabled=lambda ctx, agent: False)
    base = cc.estimate_tools_tokens([on])
    assert cc.estimate_tools_tokens([on, off, cb_off]) == base
    assert cc.estimate_tools_tokens([on, cb_on]) > base
    # all filtered out → 0, no boilerplate base either
    assert cc.estimate_tools_tokens([off, cb_off]) == 0


def test_estimate_tools_counts_weird_is_enabled_conservatively():
    # A raising or non-bool callback must count the tool: estimating high is
    # safe, estimating low breaks the compaction pre-check.
    def boom(ctx, agent):
        raise RuntimeError("x")
    async def coro(ctx, agent):
        return False
    t1 = _FakeTool("a", is_enabled=boom)
    t2 = _FakeTool("b", is_enabled=coro)   # returns a coroutine, not a bool
    assert cc.estimate_tools_tokens([t1]) > 0
    assert cc.estimate_tools_tokens([t2]) > 0
