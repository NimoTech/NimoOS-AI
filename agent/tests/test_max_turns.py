# NimoOS-AI/agent/tests/test_max_turns.py
import time
import db as db_module
from main import _read_max_turns_setting


def _conn(tmp_path):
    return db_module.init_db(str(tmp_path / "t.db"))


def _set(conn, user_id, value):
    conn.execute(
        "INSERT INTO user_settings(user_id, key, value, updated_at) "
        "VALUES(?, 'max_turns_default', ?, ?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
        (user_id, str(value), int(time.time())),
    )
    conn.commit()


def test_default_is_10_when_unset(tmp_path):
    conn = _conn(tmp_path)
    assert _read_max_turns_setting(conn, "u1") == 10


def test_reads_stored_value(tmp_path):
    conn = _conn(tmp_path)
    _set(conn, "u1", 25)
    assert _read_max_turns_setting(conn, "u1") == 25


def test_zero_means_unlimited_sentinel(tmp_path):
    conn = _conn(tmp_path)
    _set(conn, "u1", 0)
    assert _read_max_turns_setting(conn, "u1") == 0


def test_garbage_value_falls_back_to_10(tmp_path):
    conn = _conn(tmp_path)
    _set(conn, "u1", "abc")
    assert _read_max_turns_setting(conn, "u1") == 10
