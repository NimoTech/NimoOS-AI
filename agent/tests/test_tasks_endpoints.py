# NimoOS-AI/agent/tests/test_tasks_endpoints.py
"""HTTP surface for scheduled tasks (M2 task 7).

Every endpoint is user-scoped through `X-User-Id`; a task belonging to
someone else must look *absent* (404), never forbidden (403), so the API
cannot be used to probe for other users' task ids.

No `with TestClient(...)`: the repo-wide convention here is to construct the
client bare so lifespan/startup never runs (it would touch the MCP session
manager singleton and start the scheduler/runner workers).

**These tests must never touch `main._conn`.**  `conftest.py` only does
`os.environ.setdefault("AGENT_DB_PATH", ":memory:")`, and inside the agent
container that variable is already set to `/var/lib/nimoos/ai/agent/agent.db`
— the live database — so `setdefault` is a no-op and `main._conn` is the
production connection.  An earlier version of this file cleaned its tables
through it and destroyed a user's real channel bindings.  Isolation therefore
uses the repo's existing seam (see test_context_usage_endpoint.py): point
`main._DB_PATH` at a tmp file and let `main._db()` open a fresh DB, which is
what every endpoint below calls.  The fixture asserts the connection is not
the live one, so this can never silently regress.
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
                                   "mcp_tools": [], "fs_write": []}
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
