# tests/test_model_windows_endpoint.py
import pytest
from fastapi.testclient import TestClient

import context_compaction as cc
import model_windows as mw


@pytest.fixture
def client():
    import main
    main._conn.execute("DELETE FROM model_windows WHERE model_key IN ('cloud:m-ep','local:l-ep')")
    main._conn.execute("DELETE FROM user_settings WHERE user_id='mw-u1' AND key='context_window'")
    main._conn.commit()
    yield TestClient(main.app)
    main._conn.execute("DELETE FROM model_windows WHERE model_key IN ('cloud:m-ep','local:l-ep')")
    main._conn.execute("DELETE FROM user_settings WHERE user_id='mw-u1' AND key='context_window'")
    main._conn.commit()


H = {"X-User-Id": "mw-u1"}


def test_get_default_then_put_manual_then_delete(client):
    r = client.get("/agent/model-windows", params={"model": "cloud:4:m-ep"}, headers=H)
    assert r.status_code == 200
    assert r.json() == {"model_key": "cloud:m-ep", "window": cc.CLOUD_CONTEXT_WINDOW, "source": "default", "stored": None}
    r = client.put("/agent/model-windows", json={"model": "cloud:4:m-ep", "window": 64000}, headers=H)
    assert r.status_code == 200 and r.json()["source"] == "manual" and r.json()["window"] == 64000
    r = client.get("/agent/model-windows", params={"model": "m-ep", "provider_type": "other"}, headers=H)
    assert r.json()["window"] == 64000 and r.json()["source"] == "manual" and r.json()["stored"] == {"window": 64000, "source": "manual"}
    r = client.put("/agent/model-windows", json={"model": "cloud:4:m-ep", "window": 0}, headers=H)
    # Final review Minor 5: clearing the manual row is not "no window in
    # force" — report the truth (here the tier default) instead of a
    # misleading null.
    assert r.status_code == 200 and r.json()["window"] == cc.CLOUD_CONTEXT_WINDOW and r.json()["source"] == "default"
    assert client.get("/agent/model-windows", params={"model": "cloud:4:m-ep"}, headers=H).json()["source"] == "default"


def test_put_zero_with_no_manual_row_reports_the_surviving_learned_window(client):
    # Final review Minor 5: model_windows stores one (window, source) row per
    # key, so `delete_manual` (scoped to `source='manual'`) is a no-op when
    # the model never had a manual override — e.g. it auto-learned a smaller
    # window from a real context-400 rescue. The endpoint used to report
    # {"window": null, "source": null} unconditionally on window==0
    # regardless of what (if anything) actually got deleted; it must instead
    # report the truth: the learned row is still the effective window.
    import main
    mw.upsert(main._conn, "cloud:m-ep", 40_000, "learned")
    r = client.put("/agent/model-windows", json={"model": "cloud:4:m-ep", "window": 0}, headers=H)
    assert r.status_code == 200 and r.json() == {"status": "ok", "model_key": "cloud:m-ep", "window": 40_000, "source": "learned"}
    assert mw.get(main._conn, "cloud:m-ep")["source"] == "learned"          # untouched by delete_manual


def test_put_rejects_small_large_empty_model_and_requires_user(client):
    r = client.put("/agent/model-windows", json={"model": "cloud:4:m-ep", "window": cc.MIN_CONTEXT_WINDOW - 1}, headers=H)
    assert r.status_code == 400 and r.json()["detail"] == "window_too_small"
    # Final review Minor 4: no upper bound previously meant a typo (e.g. one
    # extra digit) was accepted silently and made compaction inert.
    r = client.put("/agent/model-windows", json={"model": "cloud:4:m-ep", "window": cc.MAX_CONTEXT_WINDOW + 1}, headers=H)
    assert r.status_code == 400 and r.json()["detail"] == "window_too_large"
    # Final review Minor 6: an empty model must not write a junk "cloud:" key.
    r = client.put("/agent/model-windows", json={"model": "  ", "window": 9000}, headers=H)
    assert r.status_code == 400
    assert client.get("/agent/model-windows", params={"model": "x"}).status_code == 401
    assert client.put("/agent/model-windows", json={"model": "x", "window": 9000}).status_code == 401
    assert client.get("/agent/model-windows", headers=H).status_code == 400          # model required


def test_local_selector_uses_local_tier(client):
    r = client.get("/agent/model-windows", params={"model": "local:l-ep"}, headers=H)
    assert r.json() == {"model_key": "local:l-ep", "window": cc.LOCAL_CONTEXT_WINDOW, "source": "default", "stored": None}
