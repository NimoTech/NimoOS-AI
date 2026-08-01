import importlib
import db as db_module
import phoenix_tracing as pt


def _conn():
    return db_module.init_db(":memory:")


def test_global_setting_default_false():
    m = importlib.reload(pt)
    conn = _conn()
    assert m.tracing_globally_enabled(conn) is False


def test_set_and_read_global_setting():
    m = importlib.reload(pt)
    conn = _conn()
    m.set_tracing_globally_enabled(conn, True)
    assert m.tracing_globally_enabled(conn) is True
    assert m.tracing_enabled_now() is True          # write syncs the in-process flag
    m.set_tracing_globally_enabled(conn, False)
    assert m.tracing_globally_enabled(conn) is False
    assert m.tracing_enabled_now() is False


def test_global_row_independent_of_user():
    m = importlib.reload(pt)
    conn = _conn()
    m.set_tracing_globally_enabled(conn, True)
    # a real user without their own row still reads the global as True
    row = conn.execute(
        "SELECT value FROM user_settings WHERE user_id='__global__' AND key='tracing_enabled'"
    ).fetchone()
    assert row["value"] == "1"


def test_refresh_flag_from_db():
    m = importlib.reload(pt)
    conn = _conn()
    conn.execute(
        "INSERT INTO user_settings(user_id,key,value,updated_at) "
        "VALUES('__global__','tracing_enabled','1',0)")
    conn.commit()
    m._set_flag(False)
    m.refresh_enabled_flag(conn)
    assert m.tracing_enabled_now() is True


from fastapi.testclient import TestClient


def test_tracing_settings_endpoints(monkeypatch):
    import main as mainmod
    client = TestClient(mainmod.app)
    h = {"X-User-Id": "u1"}
    assert client.get("/agent/user-settings/tracing", headers=h).json() == {"enabled": False}
    r = client.put("/agent/user-settings/tracing", headers=h, json={"enabled": True})
    assert r.status_code == 200
    assert client.get("/agent/user-settings/tracing", headers=h).json() == {"enabled": True}
    # another user sees the same global value
    assert client.get("/agent/user-settings/tracing",
                      headers={"X-User-Id": "u2"}).json() == {"enabled": True}
