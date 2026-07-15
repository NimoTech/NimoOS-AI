import json

import pytest

import context_compaction as cc
from db import init_db


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "m.db"))
    yield c
    c.close()


def _sess(conn, sid="s1", user="u1", summary=None):
    conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at,rolling_summary) "
                 "VALUES(?,?,0,0,?)", (sid, user, summary))
    conn.commit()


def _snapshot(conn, sid, history):
    conn.execute("INSERT INTO messages(id,session_id,role,content,created_at) "
                 "VALUES(?,?,?,?,?)", (sid + "-m", sid, "history",
                                       json.dumps(history), 1))
    conn.commit()


def test_empty_session_zero(conn):
    _sess(conn)
    u = cc.compute_usage(conn, session_id="s1", user_id="u1", model="gpt-4o")
    assert u["tokens"] == 0 and u["pct"] == 0
    assert u["window"] == 128000


def test_history_counts_tokens_and_pct(conn):
    _sess(conn)
    _snapshot(conn, "s1", [{"role": "user", "content": "你好" * 50},
                           {"role": "assistant", "content": "回答" * 50}])
    u = cc.compute_usage(conn, session_id="s1", user_id="u1", model="qwen")
    assert u["tokens"] > 0
    assert u["window"] == 32768
    assert u["pct"] == round(100 * u["tokens"] / u["window"])


def test_rolling_summary_is_counted(conn):
    _sess(conn, summary="这是一段较长的对话历史摘要" * 20)
    _snapshot(conn, "s1", [{"role": "user", "content": "新问题"}])
    with_sum = cc.compute_usage(conn, session_id="s1", user_id="u1", model="qwen")
    conn.execute("UPDATE sessions SET rolling_summary=NULL WHERE id='s1'"); conn.commit()
    without = cc.compute_usage(conn, session_id="s1", user_id="u1", model="qwen")
    assert with_sum["tokens"] > without["tokens"]


def test_model_changes_window(conn):
    _sess(conn)
    _snapshot(conn, "s1", [{"role": "user", "content": "x" * 4000}])
    big = cc.compute_usage(conn, session_id="s1", user_id="u1", model="gpt-4o")
    unknown = cc.compute_usage(conn, session_id="s1", user_id="u1", model="some-local")
    assert big["window"] == 128000 and unknown["window"] == cc.DEFAULT_CONTEXT_WINDOW
    assert unknown["pct"] > big["pct"]   # same tokens, smaller window → higher pct


def test_user_context_window_override(conn):
    _sess(conn)
    conn.execute("INSERT INTO user_settings(user_id,key,value,updated_at) "
                 "VALUES('u1','context_window','5000',0)"); conn.commit()
    _snapshot(conn, "s1", [{"role": "user", "content": "x" * 100}])
    u = cc.compute_usage(conn, session_id="s1", user_id="u1", model="gpt-4o")
    assert u["window"] == 5000


def test_cross_user_session_returns_zero(conn):
    # IDOR guard: a session owned by u1 must not leak its usage to u2.
    _sess(conn, sid="s1", user="u1")
    _snapshot(conn, "s1", [{"role": "user", "content": "机密" * 100}])
    owner = cc.compute_usage(conn, session_id="s1", user_id="u1", model="qwen")
    other = cc.compute_usage(conn, session_id="s1", user_id="u2", model="qwen")
    assert owner["tokens"] > 0
    assert other["tokens"] == 0 and other["pct"] == 0   # not owned → zeros


def test_usage_excludes_folded_prefix(tmp_path):
    conn = init_db(str(tmp_path / "m.db"))
    conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at,"
                 "rolling_summary,folded_upto,last_overhead_tokens) "
                 "VALUES('s1','u1',0,0,'SUM',2,0)")
    hist = [{"role": "user", "content": "a" * 400},
            {"role": "assistant", "content": "b" * 400},
            {"role": "user", "content": "c" * 40}]
    conn.execute("INSERT INTO messages(id,session_id,role,content,created_at) "
                 "VALUES('m1','s1','history',?,1)", (json.dumps(hist),))
    conn.commit()
    out = cc.compute_usage(conn, session_id="s1", user_id="u1", model="m")
    # folded first two messages (~800 chars) must NOT be counted
    assert out["tokens"] < cc.estimate_messages_tokens(hist)
    expected = (cc.estimate_tokens("SUM")
                + cc.estimate_messages_tokens(hist[2:]))
    assert abs(out["tokens"] - expected) <= 2
