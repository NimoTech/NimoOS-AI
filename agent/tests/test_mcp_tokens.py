# NimoOS-AI/agent/tests/test_mcp_tokens.py
import sqlite3
import mcp_tokens


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE mcp_tokens (
        id           TEXT PRIMARY KEY,
        user_id      TEXT NOT NULL,
        label        TEXT NOT NULL DEFAULT '',
        token_hash   TEXT NOT NULL UNIQUE,
        created_at   INTEGER NOT NULL,
        last_used_at INTEGER,
        revoked      INTEGER NOT NULL DEFAULT 0
    );
    """)
    return c


def test_generate_token_has_prefix_and_entropy():
    t1 = mcp_tokens.generate_token()
    t2 = mcp_tokens.generate_token()
    assert t1.startswith("nimoos_mcp_") and len(t1) >= 11 + 32
    assert t1 != t2


def test_create_then_verify_roundtrip():
    c = _conn()
    tok_id, plain = mcp_tokens.create(c, "42", "laptop", now_ms=1000)
    assert mcp_tokens.verify(c, plain, now_ms=2000) == "42"
    assert mcp_tokens.verify(c, "nimoos_mcp_wrong", now_ms=2000) is None


def test_revoke_blocks_verify():
    c = _conn()
    tok_id, plain = mcp_tokens.create(c, "42", "laptop", now_ms=1000)
    assert mcp_tokens.revoke(c, "42", tok_id) is True
    assert mcp_tokens.verify(c, plain, now_ms=2000) is None
    # wrong user cannot revoke someone else's token
    tok_id2, _ = mcp_tokens.create(c, "42", "x", now_ms=1000)
    assert mcp_tokens.revoke(c, "99", tok_id2) is False


def test_list_for_user_hides_secrets():
    c = _conn()
    mcp_tokens.create(c, "42", "laptop", now_ms=1000)
    rows = mcp_tokens.list_for_user(c, "42")
    assert len(rows) == 1
    assert set(rows[0]) == {"id", "label", "created_at", "last_used_at"}
