"""M4:把飞书注册成一个可寻址的通知目标。

不做配对制(spec §0:本期不在飞书里与 agent 对话),但 `tasks/notify.py` 的
目标解析要求 `channel_chats` → `channel_bindings` → user_id 这条链存在。所以
enable() 合成那两行:binding 的 external_user_id 是机器人可见的用户 open_id,
chat 的 external_chat_id 也是同一个 open_id —— 机器人无法列出自己的 p2p 会话
(`im +chat-list --types=p2p` 是 user-only),所以私聊没有 chat id 可存,直接用
open_id 寻址。

`channel_chats.session_id` 是 NOT NULL 而通知目标没有会话,填 ''。这安全的前提
是本期不消费 `im.message.receive_v1`,所以 `router.handle` 永远看不到这一行。
将来若做飞书对话,必须先给它一个真实 session。

`parse_identity` 的形状是照 `lark-cli auth status --json` 的真实输出写的,不是
`{"ok": ..., "data": ...}` 那种猜测的信封 —— 真实输出是
`{"identity": "user"|"bot", "identities": {"user": {...}, "bot": {...}}, ...}`,
当前激活身份的键名在顶层 `identity` 字段里,对应条目里才有 `openId`/`userName`
(且只在 `available` 为真时可信)。我们要的是**人**的 open_id(机器人能对其发起
私聊的那个人),所以只在激活身份是 `"user"` 且该条目可用时才返回;`"bot"` 身份
条目根本不带 openId(bot 没有可寻址的个人身份),此时视为不可用。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid

from channels import lark_cli
from channels import store as channel_store

_LOG = logging.getLogger("nimoos-agent.channels.lark_setup")

CHANNEL_TYPE = "lark"
INSTANCE_NAME = "Feishu"


class LarkSetupError(Exception):
    """Setup could not complete; nothing was written."""


def parse_identity(raw: str) -> dict | None:
    """Pull {open_id, name} for the AUTHORISED USER out of `auth status --json`.

    Reads `identities.user` directly rather than following the top-level
    `identity` pointer. That pointer names the CLI's *current default*
    identity, which may legitimately be "bot" on a setup whose user auth is
    perfectly usable — and following it would address notifications at the
    bot itself the day a lark-cli build starts putting an openId on the bot
    entry. A notification is the bot DMing the human, so the target is
    always the person's open_id.
    """
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(doc, dict):
        return None
    identities = doc.get("identities")
    if not isinstance(identities, dict):
        return None
    user = identities.get("user")
    if not isinstance(user, dict) or not user.get("available"):
        return None
    open_id = user.get("openId")
    if not isinstance(open_id, str) or not open_id:
        return None
    name = user.get("userName")
    return {"open_id": open_id, "name": name if isinstance(name, str) else ""}


async def resolve_bot_identity(uid: str) -> dict | None:
    """Who the bot can address for this user, per lark-cli. None if unusable."""
    rc, out, err = await lark_cli.run_once(uid, ["auth", "status", "--json"],
                                          timeout=15.0)
    if rc != 0:
        _LOG.warning("lark setup: auth status rc=%s: %s", rc, (err or "")[:200])
        return None
    return parse_identity(out)


def _find_instance(conn, uid: str):
    for row in channel_store.list_instances(conn):
        if row["channel_type"] != CHANNEL_TYPE:
            continue
        try:
            cfg = json.loads(row["config_json"])
        except (ValueError, TypeError):
            continue
        if str(cfg.get("uid") or "") == str(uid):
            return row
    return None


def _find_binding(conn, instance_id: str, uid: str):
    return conn.execute(
        "SELECT * FROM channel_bindings WHERE instance_id=? AND user_id=?",
        (instance_id, str(uid))).fetchone()


async def enable(conn, uid: str, *, now_ms: int) -> dict:
    """Create (or refresh) the addressable Feishu target for `uid`.

    Idempotent: repeated calls reuse the same instance and binding, and only
    refresh the identity. Raises LarkSetupError without writing anything when
    the CLI cannot tell us who the bot is.
    """
    identity = await resolve_bot_identity(uid)
    if identity is None:
        raise LarkSetupError("lark-cli is unavailable or not logged in")

    row = _find_instance(conn, uid)
    if row is None:
        inst = channel_store.create_instance(
            conn, CHANNEL_TYPE, INSTANCE_NAME,
            {"uid": str(uid), "event_key": "card.action.trigger"},
            created_by=str(uid), now_ms=now_ms)
        instance_id = inst["id"]
    else:
        instance_id = row["id"]
        channel_store.set_instance_enabled(conn, instance_id, True,
                                           now_ms=now_ms)

    binding = _find_binding(conn, instance_id, uid)
    if binding is None:
        binding_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO channel_bindings (id, instance_id, external_user_id, "
            "external_username, user_id, revoked, created_at) "
            "VALUES (?,?,?,?,?,0,?)",
            (binding_id, instance_id, identity["open_id"], identity["name"],
             str(uid), now_ms))
    else:
        binding_id = binding["id"]
        conn.execute(
            "UPDATE channel_bindings SET external_user_id=?, "
            "external_username=?, revoked=0 WHERE id=?",
            (identity["open_id"], identity["name"], binding_id))
    conn.commit()

    # session_id='' — see the module docstring.
    channel_store.upsert_chat(conn, instance_id, identity["open_id"],
                              binding_id, "", now_ms)
    return {"instance_id": instance_id, "open_id": identity["open_id"],
            "name": identity["name"]}


def disable(conn, uid: str) -> bool:
    """Stop the adapter and take the target out of every picker.

    Disabling the instance is enough: `list_chats_for_user` joins on
    `i.enabled=1`, and `ChannelManager.reload()` stops adapters for disabled
    instances. The rows stay so run history keeps resolving.
    """
    row = _find_instance(conn, uid)
    if row is None:
        return False
    channel_store.set_instance_enabled(conn, row["id"], False,
                                       now_ms=row["updated_at"])
    return True


def status(conn, uid: str) -> dict:
    row = _find_instance(conn, uid)
    if row is None:
        return {"enabled": False, "instance_id": "", "open_id": "", "name": ""}
    binding = _find_binding(conn, row["id"], uid)
    # A revoked binding makes the target unresolvable in tasks/notify.py, so
    # reporting "enabled" off the instance flag alone would promise delivery
    # that cannot happen.
    revoked = bool(binding["revoked"]) if binding is not None else True
    return {
        "enabled": bool(row["enabled"]) and not revoked,
        "instance_id": row["id"],
        "open_id": binding["external_user_id"] if binding else "",
        "name": (binding["external_username"] or "") if binding else "",
    }
