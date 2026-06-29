import memory_store
from db import init_db


def _set(conn, uid, key, val):
    conn.execute("INSERT INTO user_settings(user_id,key,value,updated_at) "
                 "VALUES(?,?,?,0)", (uid, key, val))
    conn.commit()


def test_compaction_enabled_defaults_true(tmp_path):
    conn = init_db(str(tmp_path / "m.db"))
    assert memory_store.is_compaction_enabled(conn, "u1") is True
    conn.close()


def test_compaction_enabled_respects_zero(tmp_path):
    conn = init_db(str(tmp_path / "m.db"))
    _set(conn, "u1", "compaction_enabled", "0")
    assert memory_store.is_compaction_enabled(conn, "u1") is False
    conn.close()


def test_get_context_window_valid_and_invalid(tmp_path):
    conn = init_db(str(tmp_path / "m.db"))
    assert memory_store.get_context_window(conn, "u1") is None
    _set(conn, "u1", "context_window", "32768")
    assert memory_store.get_context_window(conn, "u1") == 32768
    conn.execute("UPDATE user_settings SET value='0' WHERE user_id='u1' "
                 "AND key='context_window'"); conn.commit()
    assert memory_store.get_context_window(conn, "u1") is None      # <=0 invalid
    conn.execute("UPDATE user_settings SET value='abc' WHERE user_id='u1' "
                 "AND key='context_window'"); conn.commit()
    assert memory_store.get_context_window(conn, "u1") is None      # non-int
    conn.close()
