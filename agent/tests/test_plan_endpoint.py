# tests/test_plan_endpoint.py
import json

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client():
    main._conn.execute("DELETE FROM sessions WHERE id IN ('plan-s1','plan-s2')")
    main._conn.execute(
        "INSERT INTO sessions(id,user_id,created_at,updated_at,source,plan_json) "
        "VALUES('plan-s1','u1',0,0,'web',?)",
        (json.dumps([{"id": "a", "title": "T", "status": "done", "note": ""}]),))
    main._conn.commit()
    yield TestClient(main.app)
    main._conn.execute("DELETE FROM sessions WHERE id IN ('plan-s1','plan-s2')")
    main._conn.commit()


def test_get_plan_returns_steps_for_owner(client):
    r = client.get("/agent/sessions/plan-s1/plan", headers={"X-User-Id": "u1"})
    assert r.status_code == 200 and r.json() == {"steps": [{"id": "a", "title": "T", "status": "done", "note": ""}]}


def test_get_plan_404_for_other_user_or_missing(client):
    assert client.get("/agent/sessions/plan-s1/plan", headers={"X-User-Id": "u2"}).status_code == 404
    assert client.get("/agent/sessions/plan-nope/plan", headers={"X-User-Id": "u1"}).status_code == 404
