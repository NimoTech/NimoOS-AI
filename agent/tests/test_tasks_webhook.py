# NimoOS-AI/agent/tests/test_tasks_webhook.py
"""M3: webhook trigger — `POST /agent/task-webhook/{token}`.

This endpoint is the one place in the agent API with **no JWT**: the Go layer
skips authentication for it and strips every identity header, so the task's
own `webhook_token` is the entire credential and the user is resolved from the
token alone.  Two properties therefore have to hold no matter what:

* the caller never chooses whose task runs — an `X-User-Id` header on the wire
  must be ignored, not trusted;
* nothing the caller sends reaches the agent.  The run is queued from the
  stored prompt; the request body is not parsed at all (spec §9: "第一期不接受
  任何参数注入 prompt").

DB isolation uses the repo's existing seam — `main._DB_PATH` monkeypatched at
run time — and never `main._conn`, which inside the container is the live
production database.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

import main
from tasks import store

H = {"X-User-Id": "u1"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_DB_PATH", str(tmp_path / "agent.db"))
    conn = main._db()
    assert conn is not main._conn, (
        "webhook tests must run against an isolated DB, never the connection "
        "main.py opened at import time")
    from tasks import webhook as _webhook
    _webhook.RATE_LIMITER.reset()
    yield TestClient(main.app), conn


def _make_task(conn, *, user_id="u1", enabled=1, name="t"):
    task_id = store.create_task(
        conn, user_id, name=name, prompt="do the thing",
        trigger_type="webhook_only",
    )
    # create_task always inserts enabled=1 (a task is born on); flip it through
    # the normal update path rather than writing the column behind its back.
    if not enabled:
        conn.execute("UPDATE scheduled_tasks SET enabled=0 WHERE id=?", (task_id,))
        conn.commit()
    return store.get_task(conn, task_id, user_id)


def _runs(conn):
    return conn.execute(
        "SELECT task_id, user_id, trigger, status FROM task_runs").fetchall()


def test_valid_token_queues_exactly_one_webhook_run(client):
    c, conn = client
    task = _make_task(conn)

    r = c.post(f"/agent/task-webhook/{task['webhook_token']}")

    assert r.status_code == 202
    assert r.json()["run_id"]
    rows = _runs(conn)
    assert len(rows) == 1
    assert rows[0]["task_id"] == task["id"]
    assert rows[0]["user_id"] == "u1"
    assert rows[0]["trigger"] == "webhook"
    assert rows[0]["status"] == "queued"


def test_unknown_token_is_404_and_queues_nothing(client):
    c, conn = client
    _make_task(conn)

    r = c.post("/agent/task-webhook/" + "0" * 32)

    assert r.status_code == 404
    assert _runs(conn) == []


def test_disabled_task_does_not_fire(client):
    """Manual run deliberately works on a disabled task (that is how a user
    tests one). A webhook is unattended, so disabled means disabled."""
    c, conn = client
    task = _make_task(conn, enabled=0)

    r = c.post(f"/agent/task-webhook/{task['webhook_token']}")

    assert r.status_code == 409
    assert _runs(conn) == []


def test_second_trigger_inside_the_window_is_rate_limited(client):
    c, conn = client
    task = _make_task(conn)
    url = f"/agent/task-webhook/{task['webhook_token']}"

    assert c.post(url).status_code == 202
    r2 = c.post(url)

    assert r2.status_code == 429
    assert len(_runs(conn)) == 1


def test_rate_limit_is_per_task_not_global(client):
    c, conn = client
    a = _make_task(conn, name="a")
    b = _make_task(conn, name="b")

    assert c.post(f"/agent/task-webhook/{a['webhook_token']}").status_code == 202
    assert c.post(f"/agent/task-webhook/{b['webhook_token']}").status_code == 202
    assert len(_runs(conn)) == 2


def test_request_body_is_ignored_entirely(client):
    """No parameter injection in phase one: a body must not be parsed, and a
    malformed one must not turn a legitimate trigger into a 400."""
    c, conn = client
    task = _make_task(conn)

    r = c.post(f"/agent/task-webhook/{task['webhook_token']}",
               data="{not json at all",
               headers={"Content-Type": "application/json"})

    assert r.status_code == 202
    assert len(_runs(conn)) == 1


def test_an_identity_header_on_the_wire_cannot_choose_the_owner(client):
    """The Go layer strips these, but the endpoint must not depend on that:
    ownership comes from the token's task, never from a header."""
    c, conn = client
    task = _make_task(conn, user_id="u1")

    r = c.post(f"/agent/task-webhook/{task['webhook_token']}",
               headers={"X-User-Id": "u2", "X-NimoOS-User-ID": "2"})

    assert r.status_code == 202
    rows = _runs(conn)
    assert len(rows) == 1
    assert rows[0]["user_id"] == "u1"


def test_token_reset_invalidates_the_old_token(client):
    c, conn = client
    task = _make_task(conn)
    old = task["webhook_token"]

    r = c.post(f"/agent/tasks/{task['id']}/webhook-token/reset", headers=H)

    assert r.status_code == 200
    new = r.json()["webhook_token"]
    assert new and new != old
    assert c.post(f"/agent/task-webhook/{old}").status_code == 404
    assert c.post(f"/agent/task-webhook/{new}").status_code == 202


def test_token_reset_is_scoped_to_the_owner(client):
    c, conn = client
    task = _make_task(conn, user_id="u2")

    r = c.post(f"/agent/tasks/{task['id']}/webhook-token/reset", headers=H)

    assert r.status_code == 404
    assert store.get_task(conn, task["id"], "u2")["webhook_token"] == task["webhook_token"]


# --- the limiter itself: the endpoint tests cannot advance the clock ---------

def test_limiter_allows_again_once_the_window_passes():
    from tasks.webhook import RateLimiter
    lim = RateLimiter(window_seconds=10)

    assert lim.allow("t1", now=100.0) is True
    assert lim.allow("t1", now=105.0) is False
    assert lim.allow("t1", now=110.0) is True


def test_limiter_does_not_grow_without_bound():
    """A long-lived process must not keep an entry per task id ever seen —
    task ids are uuids and a deleted task's id never comes back."""
    from tasks.webhook import RateLimiter
    lim = RateLimiter(window_seconds=10)

    for i in range(100):
        lim.allow(f"t{i}", now=float(i))

    # Only the ids inside the trailing window survive.
    assert len(lim._last) <= 11
