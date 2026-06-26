import pytest
from db import init_db
import memory_store as ms


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "m.db"))
    yield c
    c.close()


def test_enabled_defaults_true_when_absent(conn):
    assert ms.is_memory_enabled(conn, "u1") is True


def test_enabled_false_when_zero(conn):
    conn.execute(
        "INSERT INTO user_settings(user_id,key,value,updated_at) "
        "VALUES('u1','memory_enabled','0',0)")
    assert ms.is_memory_enabled(conn, "u1") is False


def test_enabled_true_when_one(conn):
    conn.execute(
        "INSERT INTO user_settings(user_id,key,value,updated_at) "
        "VALUES('u1','memory_enabled','1',0)")
    assert ms.is_memory_enabled(conn, "u1") is True


def test_enabled_is_per_user(conn):
    conn.execute(
        "INSERT INTO user_settings(user_id,key,value,updated_at) "
        "VALUES('u1','memory_enabled','0',0)")
    assert ms.is_memory_enabled(conn, "u2") is True  # u2 has no row → default on
