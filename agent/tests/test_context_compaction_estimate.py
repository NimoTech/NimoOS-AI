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
            {"role": "assistant", "content": "世界"}]
    total = cc.estimate_messages_tokens(msgs)
    assert total >= cc.estimate_tokens("user: hello") - 1


def test_estimate_handles_nonstring_content():
    # multimodal content (list of blocks) must not crash
    msgs = [{"role": "user",
             "content": [{"type": "input_text", "text": "查日志"}]}]
    assert cc.estimate_messages_tokens(msgs) > 0


def test_resolve_window_user_override_wins(tmp_path):
    conn = init_db(str(tmp_path / "m.db"))
    conn.execute("INSERT INTO user_settings(user_id,key,value,updated_at) "
                 "VALUES('u1','context_window','40000',0)"); conn.commit()
    assert cc.resolve_window(conn, "u1", "gpt-4o") == 40000
    conn.close()


def test_resolve_window_map_then_default(tmp_path):
    conn = init_db(str(tmp_path / "m.db"))
    assert cc.resolve_window(conn, "u1", "gpt-4o-mini") == 128000
    assert cc.resolve_window(conn, "u1", "deepseek-chat") == 64000
    assert cc.resolve_window(conn, "u1", "qwen2.5-7b") == 32768
    assert cc.resolve_window(conn, "u1", "some-unknown-local") == cc.DEFAULT_CONTEXT_WINDOW
    conn.close()


def test_resolve_window_short_key_boundary(tmp_path):
    conn = init_db(str(tmp_path / "m.db"))
    # real o1/o3 family names match → 128000
    assert cc.resolve_window(conn, "u1", "o1") == 128000
    assert cc.resolve_window(conn, "u1", "o1-mini") == 128000
    assert cc.resolve_window(conn, "u1", "o3-mini") == 128000
    assert cc.resolve_window(conn, "u1", "openai/o1-preview") == 128000
    # embedded 'o1'/'o3' that are NOT the o1/o3 family must NOT false-match
    assert cc.resolve_window(conn, "u1", "do3-test") == cc.DEFAULT_CONTEXT_WINDOW
    assert cc.resolve_window(conn, "u1", "no1se-model") == cc.DEFAULT_CONTEXT_WINDOW
    assert cc.resolve_window(conn, "u1", "o13b") == cc.DEFAULT_CONTEXT_WINDOW
    assert cc.resolve_window(conn, "u1", "o1pro") == cc.DEFAULT_CONTEXT_WINDOW
    # long-key substring matching still lenient (qwen2.5 glued version)
    assert cc.resolve_window(conn, "u1", "qwen2.5-7b") == 32768
    conn.close()
