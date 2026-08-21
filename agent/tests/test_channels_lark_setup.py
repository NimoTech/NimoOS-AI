# NimoOS-AI/agent/tests/test_channels_lark_setup.py
"""M4 第一段:把飞书变成一个"可寻址的通知目标"。

**为什么要合成 binding + chat 行**:`tasks/notify.py::_resolve_target` 不只解析
`<instance_id>:<chat_id>`,它还要求存在 `channel_chats` 行、并顺着 binding_id
找到未撤销的 `channel_bindings` 行核对 user_id。所以 spec §3 说的"零改动接入
notify.py"是靠合成这两行成立的,而不是给 notify 加 lark 特例。

这两张表的含义正是"哪个外部会话对应哪个 NimoOS 用户",对飞书完全成立 —— 我们
跳过的是配对**流程**,不是这层映射。
"""
import json
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

import main
from channels import lark_setup
from channels import store as channel_store


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_DB_PATH", str(tmp_path / "agent.db"))
    c = main._db()
    assert c is not main._conn, "must not touch the import-time connection"
    return c


@pytest.fixture
def identity(monkeypatch):
    state = {"open_id": "ou_abc", "name": "雷浩文", "rc": 0}

    async def _fake(uid):
        if state["rc"] != 0:
            return None
        return {"open_id": state["open_id"], "name": state["name"]}

    monkeypatch.setattr(lark_setup, "resolve_bot_identity", _fake)
    return state


NOW = 1_700_000_000_000


@pytest.mark.asyncio
async def test_enable_creates_an_addressable_target(conn, identity):
    out = await lark_setup.enable(conn, "1", now_ms=NOW)

    inst = channel_store.get_instance(conn, out["instance_id"])
    assert inst["channel_type"] == "lark"
    assert inst["enabled"] == 1
    assert json.loads(inst["config_json"])["uid"] == "1"

    # The whole point: the existing picker query now returns it.
    targets = channel_store.list_chats_for_user(conn, "1")
    assert [t["value"] for t in targets] == [f"{out['instance_id']}:ou_abc"]
    assert targets[0]["channel_type"] == "lark"
    assert targets[0]["chat_title"] == "雷浩文"


@pytest.mark.asyncio
async def test_the_target_resolves_through_the_untouched_notify_path(conn, identity):
    from tasks import notify as tasks_notify
    out = await lark_setup.enable(conn, "1", now_ms=NOW)

    resolved = tasks_notify._resolve_target(
        conn, {"user_id": "1"}, f"{out['instance_id']}:ou_abc")

    assert resolved == (out["instance_id"], "ou_abc")


@pytest.mark.asyncio
async def test_another_users_task_cannot_resolve_this_target(conn, identity):
    from tasks import notify as tasks_notify
    out = await lark_setup.enable(conn, "1", now_ms=NOW)

    assert tasks_notify._resolve_target(
        conn, {"user_id": "2"}, f"{out['instance_id']}:ou_abc") is None


@pytest.mark.asyncio
async def test_enable_is_idempotent(conn, identity):
    a = await lark_setup.enable(conn, "1", now_ms=NOW)
    b = await lark_setup.enable(conn, "1", now_ms=NOW + 1000)

    assert a["instance_id"] == b["instance_id"]
    assert len(channel_store.list_instances(conn)) == 1
    assert len(channel_store.list_chats_for_user(conn, "1")) == 1


@pytest.mark.asyncio
async def test_enable_without_a_usable_cli_changes_nothing(conn, identity):
    identity["rc"] = 1
    with pytest.raises(lark_setup.LarkSetupError):
        await lark_setup.enable(conn, "1", now_ms=NOW)
    assert channel_store.list_instances(conn) == []
    assert channel_store.list_chats_for_user(conn, "1") == []


@pytest.mark.asyncio
async def test_disable_removes_the_target_without_deleting_history(conn, identity):
    await lark_setup.enable(conn, "1", now_ms=NOW)

    assert lark_setup.disable(conn, "1") is True

    # Nothing addressable left, so a picker cannot offer a dead target...
    assert channel_store.list_chats_for_user(conn, "1") == []
    # ...but the instance row survives, so run history keeps its reference.
    assert len(channel_store.list_instances(conn)) == 1
    assert lark_setup.status(conn, "1")["enabled"] is False


@pytest.mark.asyncio
async def test_status_before_anything_is_enabled(conn):
    st = lark_setup.status(conn, "1")
    assert st["enabled"] is False
    assert st["instance_id"] == ""


@pytest.mark.asyncio
async def test_re_enable_after_disable_reuses_the_instance(conn, identity):
    first = await lark_setup.enable(conn, "1", now_ms=NOW)
    lark_setup.disable(conn, "1")
    again = await lark_setup.enable(conn, "1", now_ms=NOW + 5000)

    assert again["instance_id"] == first["instance_id"]
    assert len(channel_store.list_chats_for_user(conn, "1")) == 1


def test_identity_parsing_from_real_cli_shapes():
    """`resolve_bot_identity` parses the CLI envelope; drive the pure part.

    NOTE: this deviates from the task-3 brief. The brief guessed a
    `{"ok": ..., "data": {"open_id": ..., "name": ...}}` envelope; the real
    `lark-cli auth status --json` output (verified on-host, see task-3
    report) looks like:

        {
          "identity": "user",
          "identities": {
            "bot": {"status": "ready", "available": true},
            "user": {"status": "needs_refresh", "available": true,
                     "openId": "ou_...", "userName": "..."}
          }
        }

    i.e. no top-level "ok"/"data", camelCase "openId"/"userName" nested under
    `identities.user` (read directly, not via the top-level "identity"
    pointer — see test_identity_is_taken_from_the_user_entry_regardless_of_
    the_default below for why), gated by that entry's own "available" flag
    rather than a top-level success flag.
    """
    assert lark_setup.parse_identity(json.dumps({
        "identity": "user",
        "identities": {
            "bot": {"status": "ready", "available": True},
            "user": {"status": "needs_refresh", "available": True,
                     "openId": "ou_x", "userName": "N"},
        },
    })) == {"open_id": "ou_x", "name": "N"}

    assert lark_setup.parse_identity("not json") is None

    # "user" entry present but explicitly not available.
    assert lark_setup.parse_identity(json.dumps({
        "identity": "user",
        "identities": {"user": {"available": False, "openId": "ou_x"}},
    })) is None


def test_identity_never_addresses_the_bot_itself():
    """A notification is the bot DMing the human. If a future CLI build put an
    openId on the bot entry, following the top-level pointer would send every
    notification to the bot."""
    assert lark_setup.parse_identity(json.dumps({
        "identity": "bot",
        "identities": {"bot": {"available": True, "openId": "ou_BOT_ITSELF"}},
    })) is None


def test_identity_is_taken_from_the_user_entry_regardless_of_the_default():
    """`identity` names the CLI's current default, not which identity is usable."""
    got = lark_setup.parse_identity(json.dumps({
        "identity": "bot",
        "identities": {
            "bot": {"available": True},
            "user": {"available": True, "openId": "ou_human", "userName": "N"},
        },
    }))
    assert got == {"open_id": "ou_human", "name": "N"}


def test_identity_absent_key_is_handled():
    assert lark_setup.parse_identity(json.dumps({"identities": {}})) is None
    assert lark_setup.parse_identity(json.dumps({})) is None


@pytest.mark.asyncio
async def test_status_is_not_enabled_while_the_binding_is_revoked(conn, identity):
    """The generic bindings endpoint revokes rather than deletes; a target the
    notify path refuses must not be reported as enabled."""
    out = await lark_setup.enable(conn, "1", now_ms=NOW)
    conn.execute("UPDATE channel_bindings SET revoked=1 WHERE instance_id=?",
                 (out["instance_id"],))
    conn.commit()

    assert lark_setup.status(conn, "1")["enabled"] is False

    # And an explicit re-enable heals it.
    await lark_setup.enable(conn, "1", now_ms=NOW + 1000)
    assert lark_setup.status(conn, "1")["enabled"] is True
