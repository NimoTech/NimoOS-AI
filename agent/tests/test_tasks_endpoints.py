# NimoOS-AI/agent/tests/test_tasks_endpoints.py
"""HTTP surface for scheduled tasks (M2 task 7).

Every endpoint is user-scoped through `X-User-Id`; a task belonging to
someone else must look *absent* (404), never forbidden (403), so the API
cannot be used to probe for other users' task ids.

No `with TestClient(...)`: the repo-wide convention here is to construct the
client bare so lifespan/startup never runs (it would touch the MCP session
manager singleton and start the scheduler/runner workers).

**These tests must never touch `main._conn`.**  An earlier version of this
file cleaned its tables through it; because `conftest.py` used
`os.environ.setdefault("AGENT_DB_PATH", ":memory:")` and the agent container
already sets that variable to the live `/var/lib/nimoos/ai/agent/agent.db`,
the setdefault was a no-op, `main._conn` was the production connection, and
the cleanup destroyed a user's real channel bindings.  conftest now assigns
unconditionally, so `main._conn` is in-memory even inside the container — but
that is a backstop, not the isolation: this file uses the repo's existing seam
(see test_context_usage_endpoint.py) and points `main._DB_PATH` at a tmp file,
letting `main._db()` open a fresh DB, which is what every endpoint below calls.
The fixture still asserts the connection is not the import-time one, so a
regression in either layer fails loudly instead of writing somewhere real.
"""
import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

import main
from tasks import store

H = {"X-User-Id": "u1"}
H2 = {"X-User-Id": "u2"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_DB_PATH", str(tmp_path / "agent.db"))
    conn = main._db()
    assert conn is not main._conn, (
        "tasks endpoint tests must run against an isolated DB, never the "
        "connection main.py opened at import time")
    yield TestClient(main.app)


@pytest.fixture
def conn(client):
    """The isolated connection the endpoints are writing to. Depends on
    `client` so the _DB_PATH monkeypatch is already in place."""
    return main._db()


def _create(client, **over):
    body = {"name": "daily", "prompt": "do it", "trigger_type": "cron",
            "cron_expr": "*/5 * * * *"}
    body.update(over)
    return client.post("/agent/tasks", headers=H, json=body)


def _create_id(client, **over):
    r = _create(client, **over)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# -- CRUD --------------------------------------------------------------------

def test_create_list_get_update_delete(client):
    r = _create(client, notify_policy="always", notify_channel="inst:chat")
    assert r.status_code == 201
    tid = r.json()["id"]
    assert r.json()["preauth_report"] == {"truncated": {}, "rejected_rules": []}

    r = client.get("/agent/tasks", headers=H)
    assert r.status_code == 200
    tasks = r.json()["tasks"]
    assert [t["id"] for t in tasks] == [tid]
    assert tasks[0]["preauth"] == {"shell": [], "egress_domains": [],
                              "mcp_tools": [], "fs_write": [], "scripts": []}
    assert "preauth_json" not in tasks[0]
    assert tasks[0]["enabled"] is True

    r = client.get(f"/agent/tasks/{tid}", headers=H)
    assert r.status_code == 200
    assert r.json()["name"] == "daily"
    assert r.json()["next_run_at"] > 0

    r = client.put(f"/agent/tasks/{tid}", headers=H,
                   json={"name": "renamed", "enabled": False})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    row = client.get(f"/agent/tasks/{tid}", headers=H).json()
    assert row["name"] == "renamed" and row["enabled"] is False

    assert client.delete(f"/agent/tasks/{tid}", headers=H).status_code == 204
    assert client.get(f"/agent/tasks/{tid}", headers=H).status_code == 404
    assert client.delete(f"/agent/tasks/{tid}", headers=H).status_code == 404


def test_interval_and_webhook_only_tasks(client):
    tid = _create_id(client, trigger_type="interval", cron_expr="",
                     interval_seconds=900)
    assert client.get(f"/agent/tasks/{tid}", headers=H).json()["next_run_at"] > 0

    tid = _create_id(client, trigger_type="webhook_only", cron_expr="")
    row = client.get(f"/agent/tasks/{tid}", headers=H).json()
    assert row["next_run_at"] == 0
    assert row["webhook_token"]


# -- cross-user isolation ----------------------------------------------------

def test_other_users_task_is_invisible(client):
    tid = _create_id(client)
    assert client.get("/agent/tasks", headers=H2).json()["tasks"] == []
    assert client.get(f"/agent/tasks/{tid}", headers=H2).status_code == 404
    assert client.put(f"/agent/tasks/{tid}", headers=H2,
                      json={"name": "x"}).status_code == 404
    assert client.delete(f"/agent/tasks/{tid}", headers=H2).status_code == 404
    assert client.post(f"/agent/tasks/{tid}/run", headers=H2).status_code == 404
    assert client.get(f"/agent/tasks/{tid}/runs", headers=H2).status_code == 404
    r = client.post(f"/agent/tasks/{tid}/preauth/from-denied", headers=H2,
                    json={"run_id": "x", "index": 0})
    assert r.status_code == 404


def test_missing_user_header_is_401(client):
    assert client.get("/agent/tasks").status_code == 401


# -- validation --------------------------------------------------------------

@pytest.mark.parametrize("over,detail", [
    ({"name": "  "}, "name_required"),
    ({"prompt": ""}, "prompt_required"),
    ({"trigger_type": "martian"}, "bad_trigger_type"),
    ({"cron_expr": "not a cron"}, "bad_cron"),
    ({"cron_expr": "61 * * * *"}, "bad_cron"),
    ({"cron_expr": "0 0 30 2 *"}, "bad_cron"),          # parses, never fires
    ({"trigger_type": "interval", "cron_expr": "",
      "interval_seconds": 30}, "bad_interval"),
    ({"trigger_type": "interval", "cron_expr": ""}, "bad_interval"),
    ({"max_turns": 0}, "bad_max_turns"),
    ({"max_turns": 101}, "bad_max_turns"),
    ({"timeout_seconds": 59}, "bad_timeout"),
    ({"timeout_seconds": 7201}, "bad_timeout"),
    ({"overlap_policy": "explode"}, "bad_overlap_policy"),
    ({"catchup_policy": "explode"}, "bad_catchup_policy"),
    ({"notify_policy": "explode"}, "bad_notify_policy"),
])
def test_create_validation(client, over, detail):
    r = _create(client, **over)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == detail


def test_create_requires_name_and_prompt(client):
    r = client.post("/agent/tasks", headers=H,
                    json={"trigger_type": "cron", "cron_expr": "* * * * *"})
    assert r.status_code == 400 and r.json()["detail"] == "name_required"


def test_update_validation_uses_the_merged_state(client):
    tid = _create_id(client)
    r = client.put(f"/agent/tasks/{tid}", headers=H,
                   json={"cron_expr": "nonsense"})
    assert r.status_code == 400 and r.json()["detail"] == "bad_cron"
    # switching to interval without a period is rejected on the merged row
    r = client.put(f"/agent/tasks/{tid}", headers=H,
                   json={"trigger_type": "interval"})
    assert r.status_code == 400 and r.json()["detail"] == "bad_interval"
    r = client.put(f"/agent/tasks/{tid}", headers=H,
                   json={"trigger_type": "interval", "interval_seconds": 600})
    assert r.status_code == 200
    row = client.get(f"/agent/tasks/{tid}", headers=H).json()
    assert row["trigger_type"] == "interval" and row["interval_seconds"] == 600


# -- preauth normalization report -------------------------------------------

_REGEXY = {"shell": [{"kind": "prefix", "value": "date "},
                     {"kind": "regex", "value": "^rm .*"}],
           "egress_domains": ["example.com"], "junk": 1}


def test_create_reports_rejected_preauth_rules(client):
    r = _create(client, preauth=_REGEXY)
    assert r.status_code == 201
    report = r.json()["preauth_report"]
    assert report["rejected_rules"] == [
        {"field": "shell", "value": "^rm .*",
         "reason": "regex_rules_not_supported"}]
    row = client.get(f"/agent/tasks/{r.json()['id']}", headers=H).json()
    assert row["preauth"]["shell"] == [{"kind": "prefix", "value": "date "}]
    assert row["preauth"]["egress_domains"] == ["example.com"]


def test_update_reports_rejected_preauth_rules(client):
    tid = _create_id(client)
    r = client.put(f"/agent/tasks/{tid}", headers=H, json={"preauth": _REGEXY})
    assert r.status_code == 200
    assert r.json()["preauth_report"]["rejected_rules"][0]["value"] == "^rm .*"
    assert r.json()["status"] == "ok"


def test_update_replaces_the_whole_preauth_document(client):
    """PUT preauth is a REPLACE, not a merge — pinned so Task 8's UI has to
    send the full document rather than silently wiping the other buckets."""
    tid = _create_id(client, preauth={"egress_domains": ["a.example.com"],
                                      "mcp_tools": ["gh::list"]})
    client.put(f"/agent/tasks/{tid}", headers=H,
               json={"preauth": {"egress_domains": ["b.example.com"]}})
    doc = client.get(f"/agent/tasks/{tid}", headers=H).json()["preauth"]
    assert doc["egress_domains"] == ["b.example.com"]
    assert doc["mcp_tools"] == []


@pytest.mark.parametrize("value", ["abc", ["a"], 7, None])
def test_preauth_must_be_an_object(client, value):
    # parse() would turn any of these into an empty document and answer 201,
    # leaving the author believing the rules were accepted.
    r = _create(client, preauth=value)
    assert r.status_code == 400 and r.json()["detail"] == "bad_preauth"
    tid = _create_id(client, name="t2")
    r = client.put(f"/agent/tasks/{tid}", headers=H, json={"preauth": value})
    assert r.status_code == 400 and r.json()["detail"] == "bad_preauth"


@pytest.mark.parametrize("path", [
    "/",                    # one string = write access to the whole filesystem
    "/etc",
    "/etc/nimoos",
    "/usr/share/nimoos/agent",
    "/var/lib/nimoos/ai/agent",   # the agent's own database
    "relative/path",
    " ",
])
def test_fs_write_refuses_root_and_system_paths(client, path):
    r = _create(client, preauth={"fs_write": [path]})
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "bad_fs_write"


def test_fs_write_refuses_an_unresolvable_path(client):
    # os.path.realpath raises ValueError on an embedded NUL; unresolvable
    # means unjudgeable, and it must be a 400, not an escaped 500.
    r = _create(client, preauth={"fs_write": ["/DATA/\x00x"]})
    assert r.status_code == 400 and r.json()["detail"] == "bad_fs_write"


def test_fs_write_refuses_a_symlink_to_root(client, tmp_path):
    link = tmp_path / "innocent"
    link.symlink_to("/")
    r = _create(client, preauth={"fs_write": [str(link)]})
    assert r.status_code == 400 and r.json()["detail"] == "bad_fs_write"


def test_fs_write_accepts_a_normal_data_directory(client, tmp_path):
    r = _create(client, preauth={"fs_write": [str(tmp_path)]})
    assert r.status_code == 201


# -- manual run + history ----------------------------------------------------

def test_manual_run_enqueues_and_shows_up_in_history(client, conn):
    tid = _create_id(client)
    r = client.post(f"/agent/tasks/{tid}/run", headers=H)
    assert r.status_code == 202
    run_id = r.json()["run_id"]

    row = conn.execute("SELECT * FROM task_runs WHERE id=?",
                       (run_id,)).fetchone()
    assert row["status"] == "queued" and row["trigger"] == "manual"
    assert row["user_id"] == "u1"

    r = client.get(f"/agent/tasks/{tid}/runs", headers=H)
    assert r.status_code == 200
    runs = r.json()["runs"]
    assert len(runs) == 1 and runs[0]["id"] == run_id
    assert runs[0]["denied_actions"] == []


def test_run_history_limit(client):
    tid = _create_id(client)
    ids = [client.post(f"/agent/tasks/{tid}/run", headers=H).json()["run_id"]
           for _ in range(3)]
    runs = client.get(f"/agent/tasks/{tid}/runs?limit=2", headers=H).json()["runs"]
    assert len(runs) == 2
    assert runs[0]["id"] == ids[-1]           # newest first
    # a nonsense limit is clamped, not a 500
    assert len(client.get(f"/agent/tasks/{tid}/runs?limit=0",
                          headers=H).json()["runs"]) == 1
    assert client.get(f"/agent/tasks/{tid}/runs?limit=abc",
                      headers=H).status_code == 422


def test_manual_run_is_allowed_on_a_disabled_task(client):
    tid = _create_id(client)
    client.put(f"/agent/tasks/{tid}", headers=H, json={"enabled": False})
    assert client.post(f"/agent/tasks/{tid}/run", headers=H).status_code == 202


# -- from-denied -------------------------------------------------------------

def _run_with_denied(client, denied, tid=None):
    tid = tid or _create_id(client)
    run_id = client.post(f"/agent/tasks/{tid}/run", headers=H).json()["run_id"]
    store.finish_run(main._db(), run_id, "failed", denied=denied)
    return tid, run_id


def _from_denied(client, tid, run_id, index, headers=H):
    return client.post(f"/agent/tasks/{tid}/preauth/from-denied",
                       headers=headers, json={"run_id": run_id, "index": index})


def test_from_denied_egress(client):
    tid, run_id = _run_with_denied(
        client, [{"kind": "egress", "detail": "api.example.com:443"}])
    r = _from_denied(client, tid, run_id, 0)
    assert r.status_code == 200
    assert r.json()["preauth"]["egress_domains"] == ["api.example.com"]
    # idempotent: a second adoption does not duplicate the entry
    r = _from_denied(client, tid, run_id, 0)
    assert r.json()["preauth"]["egress_domains"] == ["api.example.com"]


def test_from_denied_fs_directory_and_file(client, tmp_path):
    d = tmp_path / "reports"
    d.mkdir()
    f = d / "out.csv"
    f.write_text("x")
    tid, run_id = _run_with_denied(client, [
        {"kind": "fs", "detail": str(d)},
        {"kind": "fs", "detail": str(f)},
    ])
    assert _from_denied(client, tid, run_id, 0).json()["preauth"]["fs_write"] \
        == [str(d)]
    # a file adopts its parent directory, which is already there -> no dupe
    assert _from_denied(client, tid, run_id, 1).json()["preauth"]["fs_write"] \
        == [str(d)]


def test_from_denied_shell_uses_the_command_head(client):
    tid, run_id = _run_with_denied(client, [
        {"kind": "shell", "detail": "lark-cli mail list --limit 5"},
        {"kind": "shell", "detail": "date"},
        {"kind": "shell", "detail": "  rm -rf /tmp/x"},
    ])
    r = _from_denied(client, tid, run_id, 0)
    assert r.json()["preauth"]["shell"] == [
        {"kind": "prefix", "value": "lark-cli "}]
    assert r.json()["adopted"] == {"field": "shell",
                                   "value": {"kind": "prefix",
                                             "value": "lark-cli "}}
    rules = _from_denied(client, tid, run_id, 1).json()["preauth"]["shell"]
    assert {"kind": "prefix", "value": "date"} in rules
    rules = _from_denied(client, tid, run_id, 2).json()["preauth"]["shell"]
    assert {"kind": "prefix", "value": "  rm "} in rules


def test_adopted_shell_rules_actually_match_the_denied_command(client):
    """The whole point of the button: after adopting, the very command that
    was denied must pass `preauth.shell_match`. `shell_match` does not strip,
    so a rule generated from a leading-whitespace command has to keep it."""
    from tasks import preauth as preauth_mod
    commands = ["lark-cli mail list", "date", "  rm -rf /tmp/x"]
    tid, run_id = _run_with_denied(
        client, [{"kind": "shell", "detail": c} for c in commands])
    for i in range(len(commands)):
        rules = _from_denied(client, tid, run_id, i).json()["preauth"]["shell"]
    for command in commands:
        assert preauth_mod.shell_match(rules, command), command


def test_from_denied_mcp_tool(client):
    tid, run_id = _run_with_denied(
        client, [{"kind": "mcp_tool", "detail": "github::create_issue"}])
    assert _from_denied(client, tid, run_id, 0).json()["preauth"]["mcp_tools"] \
        == ["github::create_issue"]


def test_from_denied_refuses_a_root_fs_path(client):
    # Adopting a denial must not be the back door that puts "/" in fs_write.
    tid, run_id = _run_with_denied(client, [{"kind": "fs", "detail": "/"},
                                            {"kind": "fs", "detail": "/etc/x"}])
    assert _from_denied(client, tid, run_id, 0).status_code == 400
    assert _from_denied(client, tid, run_id, 0).json()["detail"] == "bad_fs_write"
    assert _from_denied(client, tid, run_id, 1).json()["detail"] == "bad_fs_write"
    assert client.get(f"/agent/tasks/{tid}",
                      headers=H).json()["preauth"]["fs_write"] == []


def test_from_denied_reports_a_full_bucket_instead_of_lying(client):
    from tasks.preauth import MAX_RULES
    full = [f"h{i}.example.com" for i in range(MAX_RULES)]
    tid = _create_id(client, preauth={"egress_domains": full})
    _, run_id = _run_with_denied(
        client, [{"kind": "egress", "detail": "new.example.com"}], tid=tid)
    r = _from_denied(client, tid, run_id, 0)
    assert r.status_code == 400 and r.json()["detail"] == "preauth_full"
    # and nothing was written
    doc = client.get(f"/agent/tasks/{tid}", headers=H).json()["preauth"]
    assert doc["egress_domains"] == full


def test_from_denied_unsupported_kind(client):
    tid, run_id = _run_with_denied(
        client, [{"kind": "elicitation", "detail": "who are you"}])
    r = _from_denied(client, tid, run_id, 0)
    assert r.status_code == 400 and r.json()["detail"] == "unsupported_kind"


def test_from_denied_bad_index_and_run(client):
    tid, run_id = _run_with_denied(
        client, [{"kind": "egress", "detail": "a.example.com"}])
    assert _from_denied(client, tid, run_id, 5).status_code == 404
    assert _from_denied(client, tid, "no-such-run", 0).status_code == 404
    # a run belonging to another task of the same user is not reachable either
    other = _create_id(client, name="other")
    assert _from_denied(client, other, run_id, 0).status_code == 404


def test_from_denied_persists_and_survives_reload(client, conn):
    tid, run_id = _run_with_denied(
        client, [{"kind": "egress", "detail": "api.example.com"}])
    _from_denied(client, tid, run_id, 0)
    row = client.get(f"/agent/tasks/{tid}", headers=H).json()
    assert row["preauth"]["egress_domains"] == ["api.example.com"]
    raw = conn.execute(
        "SELECT preauth_json FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    assert json.loads(raw["preauth_json"])["egress_domains"] == ["api.example.com"]


# -- notify targets ----------------------------------------------------------

def _pair_chat(user_id="u1", chat="55501", name="family"):
    from channels import store as channel_store
    conn = main._db()
    inst = channel_store.create_instance(
        conn, "telegram", name, {"bot_token": "t"}, user_id, now_ms=1)
    code, _ = channel_store.create_pairing_code(conn, inst["id"], user_id,
                                                now_ms=1)
    binding = channel_store.redeem_pairing_code(
        conn, inst["id"], code, f"ext-{chat}", "alice", now_ms=1)
    channel_store.upsert_chat(conn, inst["id"], chat, binding["id"],
                              "sess-1", now_ms=1)
    return inst, binding


def test_notify_targets_lists_paired_chats(client):
    inst, _ = _pair_chat()
    r = client.get("/agent/tasks/notify-targets", headers=H)
    assert r.status_code == 200
    targets = r.json()["targets"]
    assert len(targets) == 1
    assert targets[0]["value"] == f"{inst['id']}:55501"
    assert targets[0]["channel_type"] == "telegram"
    assert targets[0]["instance_name"] == "family"
    # not visible to another user
    assert client.get("/agent/tasks/notify-targets",
                      headers=H2).json()["targets"] == []


def test_notify_targets_hides_revoked_bindings(client, conn):
    from channels import store as channel_store
    _, binding = _pair_chat()
    channel_store.revoke_binding(conn, "u1", binding["id"])
    assert client.get("/agent/tasks/notify-targets",
                      headers=H).json()["targets"] == []


def test_notify_targets_hides_disabled_instances(client, conn):
    from channels import store as channel_store
    inst, _ = _pair_chat()
    channel_store.set_instance_enabled(conn, inst["id"], False, now_ms=2)
    # ChannelManager only runs adapters for enabled instances, so this chat
    # would be a target nothing could ever be delivered to.
    assert client.get("/agent/tasks/notify-targets",
                      headers=H).json()["targets"] == []


def test_notify_targets_route_is_not_shadowed_by_task_id(client):
    # /agent/tasks/{task_id} must not swallow the literal path.
    _create_id(client)
    assert client.get("/agent/tasks/notify-targets", headers=H).status_code == 200


# -- reclaiming what a task produced ----------------------------------------

def _seed_finished_run(conn, task_id, *, session_id, run_id="ar-1",
                       events=3, user_id="u1"):
    """One finished run with everything a real one leaves behind: its session,
    a message, the `agent_runs` row the SSE layer writes and that row's
    `event_log` rows."""
    import time as _time
    now = int(_time.time())
    conn.execute(
        "INSERT INTO sessions (id, user_id, title, created_at, updated_at, "
        "agent_type, source) VALUES (?,?,?,?,?,?,?)",
        (session_id, user_id, None, now, now, "general", "task"))
    conn.execute(
        "INSERT INTO messages (id, session_id, role, content, created_at) "
        "VALUES (?,?,?,?,?)",
        (f"m-{session_id}", session_id, "assistant", "[]", now))
    conn.execute(
        "INSERT INTO agent_runs (id, session_id, user_id, status, "
        "user_message, created_at) VALUES (?,?,?,?,?,?)",
        (run_id, session_id, user_id, "done", "go", now))
    for seq in range(events):
        conn.execute(
            "INSERT INTO event_log (run_id, seq, payload, created_at) "
            "VALUES (?,?,?,?)", (run_id, seq, "{}", now))
    tr_id = store.create_run(conn, task_id, user_id, "cron")
    store.attach_session(conn, tr_id, session_id)
    store.finish_run(conn, tr_id, "succeeded", summary="ok")
    conn.commit()
    return tr_id


def _counts(conn, session_id, task_id, run_id="ar-1"):
    q = lambda sql, *p: conn.execute(sql, p).fetchone()[0]  # noqa: E731
    return {
        "task_runs": q("SELECT COUNT(*) FROM task_runs WHERE task_id=?", task_id),
        "sessions": q("SELECT COUNT(*) FROM sessions WHERE id=?", session_id),
        "messages": q("SELECT COUNT(*) FROM messages WHERE session_id=?", session_id),
        "agent_runs": q("SELECT COUNT(*) FROM agent_runs WHERE session_id=?", session_id),
        "event_log": q("SELECT COUNT(*) FROM event_log WHERE run_id=?", run_id),
    }


def test_delete_task_leaves_no_orphan_rows(client, conn, monkeypatch):
    """Deleting a task must reclaim its runs and everything they own.

    `task_runs` has no FK cascade and `prune_runs` only ever walks by task_id,
    so a task row deleted on its own strands all of it with nothing left
    pointing at it — the wiki `file_events` failure class.
    """
    import session_purge

    async def no_parser(user_id, session_id):
        pass
    monkeypatch.setattr(session_purge, "default_vector_cleanup", no_parser)

    tid = _create_id(client)
    _seed_finished_run(conn, tid, session_id="s-1")
    before = _counts(conn, "s-1", tid)
    assert before == {"task_runs": 1, "sessions": 1, "messages": 1,
                      "agent_runs": 1, "event_log": 3}

    assert client.delete(f"/agent/tasks/{tid}", headers=H).status_code == 204
    assert client.get(f"/agent/tasks/{tid}", headers=H).status_code == 404
    assert _counts(conn, "s-1", tid) == {"task_runs": 0, "sessions": 0,
                                         "messages": 0, "agent_runs": 0,
                                         "event_log": 0}


def test_delete_task_does_not_touch_another_users_task(client, conn, monkeypatch):
    import session_purge

    async def no_parser(user_id, session_id):
        pass
    monkeypatch.setattr(session_purge, "default_vector_cleanup", no_parser)

    tid = _create_id(client)
    _seed_finished_run(conn, tid, session_id="s-1")
    # u2 must not be able to wipe u1's history through the delete cascade.
    assert client.delete(f"/agent/tasks/{tid}", headers=H2).status_code == 404
    assert _counts(conn, "s-1", tid) == {"task_runs": 1, "sessions": 1,
                                         "messages": 1, "agent_runs": 1,
                                         "event_log": 3}


def test_task_sessions_stay_out_of_the_chat_list(client, conn):
    """A task opens a fresh session per run, titled NULL, sorted by
    updated_at — without the filter they bury the user's real conversations.
    """
    now = 1_800_000_000
    for sid, source, updated in (("s-task", "task", now + 100),
                                 ("s-web", "web", now)):
        conn.execute(
            "INSERT INTO sessions (id, user_id, title, created_at, updated_at, "
            "agent_type, source) VALUES (?,?,?,?,?,?,?)",
            (sid, "u1", None, now, updated, "general", source))
    conn.commit()

    listed = client.get("/agent/sessions", headers=H).json()
    assert [s["id"] for s in listed] == ["s-web"]


def test_delete_returns_bounded_when_vector_cleanup_is_slow(client, conn,
                                                            monkeypatch):
    """HTTP-level bound (N2): the request must not wait for every session's
    Parser round-trip. Each cleanup awaits Parser behind a 10s timeout, so 50
    kept runs is ~500s inside a DELETE whose reverse proxy has no timeout.
    """
    import asyncio
    import time as _time

    from tasks import store as _store

    import session_purge

    async def slow(user_id, session_id):
        await asyncio.sleep(0.15)
    monkeypatch.setattr(session_purge, "default_vector_cleanup", slow)
    monkeypatch.setattr(_store, "DELETE_BUDGET_SECONDS", 0.3)
    spawned = []
    monkeypatch.setattr(_store, "_spawn", spawned.append)

    tid = _create_id(client)
    for i in range(10):
        _seed_finished_run(conn, tid, session_id=f"s-{i}", run_id=f"ar-{i}",
                           events=2)

    started = _time.monotonic()
    assert client.delete(f"/agent/tasks/{tid}", headers=H).status_code == 204
    elapsed = _time.monotonic() - started

    # Unbounded this would be ~1.5s (10 x 0.15); bounded it stops at the first
    # session past 0.3s. The assertion is deliberately loose — it only has to
    # separate "bounded" from "walks all ten".
    assert elapsed < 1.0, f"DELETE took {elapsed:.2f}s — the budget did not apply"
    assert len(spawned) == 1, "the remainder must be handed to the background"
    # Collected instead of scheduled, so close it explicitly: an un-awaited
    # coroutine would otherwise warn at collection time. The continuation's
    # work is driven deterministically at the end of this test.
    spawned[0].close()

    # The state left behind is consistent and finishable: the task is gone, and
    # every run still in the table still points at a session that still exists.
    assert client.get(f"/agent/tasks/{tid}", headers=H).status_code == 404
    left = conn.execute("SELECT session_id FROM task_runs WHERE task_id=?",
                        (tid,)).fetchall()
    assert left, "the test is meaningless if the budget cleared everything"
    for row in left:
        assert conn.execute("SELECT 1 FROM sessions WHERE id=?",
                            (row["session_id"],)).fetchone() is not None

    # Finishing the purge (what the continuation does) leaves no residue.
    async def noop(user_id, session_id):
        pass
    monkeypatch.setattr(session_purge, "default_vector_cleanup", noop)
    from tasks import runner as _runner
    asyncio.run(_store.purge_runs(conn, tid, "u1",
                                  session_deleter=_runner.delete_session))
    for i in range(10):
        assert conn.execute("SELECT 1 FROM sessions WHERE id=?",
                            (f"s-{i}",)).fetchone() is None
        assert conn.execute("SELECT 1 FROM event_log WHERE run_id=?",
                            (f"ar-{i}",)).fetchone() is None
    assert conn.execute("SELECT COUNT(*) c FROM task_runs WHERE task_id=?",
                        (tid,)).fetchone()["c"] == 0


def test_notify_on_start_round_trips_through_the_api(client):
    """A checkbox that silently fails to persist is indistinguishable from a
    feature that does not work, so pin create -> read -> update -> read."""
    r = client.post("/agent/tasks", json={
        "name": "t", "prompt": "p", "trigger_type": "cron",
        "cron_expr": "0 9 * * *", "notify_on_start": True},
        headers=H)
    assert r.status_code == 201
    tid = r.json()["id"]

    got = client.get(f"/agent/tasks/{tid}", headers=H).json()
    assert got["notify_on_start"] is True

    assert client.put(f"/agent/tasks/{tid}", json={"notify_on_start": False},
                      headers=H).status_code == 200
    assert client.get(f"/agent/tasks/{tid}",
                      headers=H).json()["notify_on_start"] is False


def test_notify_on_start_defaults_to_off(client):
    r = client.post("/agent/tasks", json={
        "name": "t", "prompt": "p", "trigger_type": "cron",
        "cron_expr": "0 9 * * *"}, headers=H)
    tid = r.json()["id"]
    assert client.get(f"/agent/tasks/{tid}",
                      headers=H).json()["notify_on_start"] is False


def test_from_denied_refuses_a_chained_shell_command(client):
    """The generator works on the command's HEAD, and the run gate refuses
    chaining outright — so adopting `which x && x --version` used to write a
    `"which "` rule that could never match it. Silent no-ops are worse than
    refusals: the user walks away believing the run is authorized."""
    tid, run_id = _run_with_denied(client, [
        {"kind": "shell", "detail": "which lark-cli && lark-cli --version"}])
    r = _from_denied(client, tid, run_id, 0)
    assert r.status_code == 400
    assert r.json()["detail"] == "shell_rule_would_not_apply"
    # and it changed nothing
    doc = client.get(f"/agent/tasks/{tid}", headers=H).json()["preauth"]
    assert doc["shell"] == []


def test_from_denied_still_adopts_a_single_simple_command(client):
    tid, run_id = _run_with_denied(client, [
        {"kind": "shell", "detail": "lark-cli im +messages-send --text hi"}])
    r = _from_denied(client, tid, run_id, 0)
    assert r.status_code == 200, r.text
    assert r.json()["adopted"] == {"field": "shell",
                                   "value": {"kind": "prefix", "value": "lark-cli "}}


def test_from_denied_refuses_an_interpreter(client):
    """Same silent no-op, different cause: a run-scoped grant never covers an
    interpreter, so `"python3 "` would be written and ignored."""
    tid, run_id = _run_with_denied(client, [
        {"kind": "shell", "detail": "python3 -c 'import os'"}])
    r = _from_denied(client, tid, run_id, 0)
    assert r.status_code == 400
    assert r.json()["detail"] == "shell_rule_would_not_apply"


def test_from_denied_egress_is_unaffected_by_the_shell_check(client):
    tid, run_id = _run_with_denied(client, [
        {"kind": "egress", "detail": "open.feishu.cn:443"}])
    r = _from_denied(client, tid, run_id, 0)
    assert r.status_code == 200, r.text
    assert r.json()["adopted"] == {"field": "egress_domains",
                                   "value": "open.feishu.cn"}


# --- cron next-run preview (spec §9 编辑器"下次运行"实时预览) -----------------

def test_cron_preview_returns_next_fires(client):
    r = client.get("/agent/tasks/cron-preview", headers=H,
              params={"expr": "0 9 * * *", "count": 3})
    assert r.status_code == 200
    fires = r.json()["next"]
    assert len(fires) == 3
    import time
    now = int(time.time())
    assert all(isinstance(t, int) and t > now for t in fires)
    assert fires == sorted(fires) and len(set(fires)) == 3


def test_cron_preview_bad_expr_400(client):
    r = client.get("/agent/tasks/cron-preview", headers=H,
              params={"expr": "not a cron"})
    assert r.status_code == 400
    # `0 0 30 2 *` parses but never fires — must be a 400 too, not a hang/500
    r = client.get("/agent/tasks/cron-preview", headers=H,
              params={"expr": "0 0 30 2 *"})
    assert r.status_code == 400


def test_cron_preview_count_clamped(client):
    r = client.get("/agent/tasks/cron-preview", headers=H,
              params={"expr": "* * * * *", "count": 999})
    assert r.status_code == 200
    assert len(r.json()["next"]) <= 5


# -- continue a finished run ---------------------------------------------------

def _finished_run(conn, tid, *, status="failed", session="sess-1",
                  error="boom", summary="", user="u1"):
    rid = store.create_run(conn, tid, user, "cron")
    if session:
        store.attach_session(conn, rid, session)
        now = 1000
        conn.execute(
            "INSERT OR IGNORE INTO sessions (id, user_id, title, created_at, "
            "updated_at) VALUES (?,?,?,?,?)", (session, user, "t", now, now))
        conn.commit()
    conn.execute("UPDATE task_runs SET status=?, error=?, summary=? WHERE id=?",
                 (status, error, summary, rid))
    conn.commit()
    return rid


def test_continue_run_queues_on_parent_session(client, conn):
    tid = _create_id(client)
    rid = _finished_run(conn, tid, error="disk full")
    r = client.post(f"/agent/tasks/{tid}/runs/{rid}/continue", headers=H,
                    json={"message": "clean /tmp first"})
    assert r.status_code == 202, r.text
    row = conn.execute("SELECT * FROM task_runs WHERE id=?",
                       (r.json()["run_id"],)).fetchone()
    assert row["status"] == "queued" and row["trigger"] == "manual"
    assert row["session_id"] == "sess-1" and row["resumed_from"] == rid
    msg = row["resume_message"]
    # The supplement rides ABOVE the boilerplate; the parent's error is cited.
    assert msg.startswith("clean /tmp first")
    assert "status: failed" in msg and "disk full" in msg
    assert "update_task_prompt" in msg


def test_continue_run_empty_body_is_fine(client, conn):
    tid = _create_id(client)
    rid = _finished_run(conn, tid)
    r = client.post(f"/agent/tasks/{tid}/runs/{rid}/continue", headers=H)
    assert r.status_code == 202, r.text


def test_continue_run_user_scoped_404(client, conn):
    tid = _create_id(client)
    rid = _finished_run(conn, tid)
    r = client.post(f"/agent/tasks/{tid}/runs/{rid}/continue", headers=H2)
    assert r.status_code == 404


def test_continue_run_not_finished_409(client, conn):
    tid = _create_id(client)
    rid = store.create_run(conn, tid, "u1", "cron")  # still queued
    r = client.post(f"/agent/tasks/{tid}/runs/{rid}/continue", headers=H)
    assert r.status_code == 409 and r.json()["detail"] == "not_finished"


def test_continue_run_pruned_session_409(client, conn):
    tid = _create_id(client)
    rid = _finished_run(conn, tid, session="")  # never had a session
    r = client.post(f"/agent/tasks/{tid}/runs/{rid}/continue", headers=H)
    assert r.status_code == 409 and r.json()["detail"] == "session_pruned"
    rid2 = _finished_run(conn, tid, session="sess-gone")
    conn.execute("DELETE FROM sessions WHERE id='sess-gone'")
    conn.commit()
    r = client.post(f"/agent/tasks/{tid}/runs/{rid2}/continue", headers=H)
    assert r.status_code == 409 and r.json()["detail"] == "session_pruned"


def test_continue_run_already_active_409(client, conn):
    tid = _create_id(client)
    rid = _finished_run(conn, tid)
    first = client.post(f"/agent/tasks/{tid}/runs/{rid}/continue", headers=H)
    assert first.status_code == 202
    # The first continuation is still queued on this session.
    r = client.post(f"/agent/tasks/{tid}/runs/{rid}/continue", headers=H)
    assert r.status_code == 409 and r.json()["detail"] == "already_active"


def test_continue_run_malformed_body_400(client, conn):
    tid = _create_id(client)
    rid = _finished_run(conn, tid)
    r = client.post(f"/agent/tasks/{tid}/runs/{rid}/continue", headers=H,
                    content=b"{oops")
    assert r.status_code == 400


def test_run_out_exposes_resumed_from(client, conn):
    tid = _create_id(client)
    rid = _finished_run(conn, tid)
    client.post(f"/agent/tasks/{tid}/runs/{rid}/continue", headers=H)
    runs = client.get(f"/agent/tasks/{tid}/runs", headers=H).json()["runs"]
    marks = {r["id"]: r.get("resumed_from") for r in runs}
    assert marks[rid] == "" and any(v == rid for v in marks.values())
