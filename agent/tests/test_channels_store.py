import db as db_module
from channels import store


def _conn(tmp_path):
    return db_module.init_db(str(tmp_path / "t.db"),
                             snapshots_root=str(tmp_path / "snaps"))


def test_instance_crud(tmp_path):
    c = _conn(tmp_path)
    row = store.create_instance(c, "telegram", "family bot",
                                {"bot_token": "123:abc"}, "u1", now_ms=1000)
    assert row["channel_type"] == "telegram" and row["enabled"] == 1
    assert store.get_instance(c, row["id"])["name"] == "family bot"
    assert len(store.list_instances(c)) == 1
    assert store.set_instance_enabled(c, row["id"], False, now_ms=2000) is True
    assert store.get_instance(c, row["id"])["enabled"] == 0
    assert store.delete_instance(c, row["id"]) is True
    assert store.list_instances(c) == []


def test_pairing_roundtrip_one_time_and_expiry(tmp_path):
    c = _conn(tmp_path)
    inst = store.create_instance(c, "telegram", "", {"bot_token": "t"}, "u1", 0)
    code, expires = store.create_pairing_code(c, inst["id"], "u1", now_ms=1000)
    assert len(code) == 8 and code.isdigit()
    assert expires == 1000 + store.PAIRING_TTL_MS
    # wrong code
    assert store.redeem_pairing_code(c, inst["id"], "00000000", "tg9", None, 2000) is None
    # good code
    b = store.redeem_pairing_code(c, inst["id"], code, "tg9", "alice", 2000)
    assert b["user_id"] == "u1" and b["external_username"] == "alice"
    # one-time
    assert store.redeem_pairing_code(c, inst["id"], code, "tg8", None, 3000) is None
    # expired
    code2, _ = store.create_pairing_code(c, inst["id"], "u1", now_ms=1000)
    assert store.redeem_pairing_code(
        c, inst["id"], code2, "tg8", None, 1000 + store.PAIRING_TTL_MS + 1) is None


def test_repair_upserts_binding_and_clears_revoked(tmp_path):
    c = _conn(tmp_path)
    inst = store.create_instance(c, "telegram", "", {"bot_token": "t"}, "u1", 0)
    code, _ = store.create_pairing_code(c, inst["id"], "u1", 1000)
    b1 = store.redeem_pairing_code(c, inst["id"], code, "tg9", "alice", 2000)
    assert store.revoke_binding(c, "u1", b1["id"]) is True
    assert store.get_binding(c, inst["id"], "tg9") is None
    # re-pair same external user to another NimoOS user
    code2, _ = store.create_pairing_code(c, inst["id"], "u2", 3000)
    b2 = store.redeem_pairing_code(c, inst["id"], code2, "tg9", "alice", 4000)
    assert b2["user_id"] == "u2" and b2["revoked"] == 0


def test_binding_model_and_user_scoping(tmp_path):
    c = _conn(tmp_path)
    inst = store.create_instance(c, "telegram", "", {"bot_token": "t"}, "u1", 0)
    code, _ = store.create_pairing_code(c, inst["id"], "u1", 1000)
    b = store.redeem_pairing_code(c, inst["id"], code, "tg9", None, 2000)
    assert store.set_binding_model(c, "u1", b["id"], "cloud:6:deepseek-chat") is True
    assert store.get_binding(c, inst["id"], "tg9")["default_model"] == "cloud:6:deepseek-chat"
    # another user cannot touch it
    assert store.set_binding_model(c, "u2", b["id"], "x") is False
    assert store.revoke_binding(c, "u2", b["id"]) is False
    assert [x["id"] for x in store.list_bindings_for_user(c, "u1")] == [b["id"]]


def test_chat_mapping_and_channel_session(tmp_path):
    c = _conn(tmp_path)
    sid = store.create_channel_session(c, "u1", "telegram")
    row = c.execute("SELECT user_id, agent_type, source FROM sessions WHERE id=?",
                    (sid,)).fetchone()
    assert (row["user_id"], row["agent_type"], row["source"]) == ("u1", "general", "telegram")
    assert store.get_chat(c, "i1", "chat9") is None
    store.upsert_chat(c, "i1", "chat9", "b1", sid, 1000)
    assert store.get_chat(c, "i1", "chat9")["session_id"] == sid
    sid2 = store.create_channel_session(c, "u1", "telegram")
    store.upsert_chat(c, "i1", "chat9", "b1", sid2, 2000)
    assert store.get_chat(c, "i1", "chat9")["session_id"] == sid2


def test_set_binding_download_dir(tmp_path):
    c = _conn(tmp_path)
    inst = store.create_instance(c, "telegram", "", {"bot_token": "t"}, "u1", 0)
    code, _ = store.create_pairing_code(c, inst["id"], "u1", now_ms=0)
    b = store.redeem_pairing_code(c, inst["id"], code, "tg1", "a", now_ms=0)
    assert store.set_binding_download_dir(c, "u1", b["id"], "/DATA/Downloads/telegram") is True
    assert store.get_binding(c, inst["id"], "tg1")["download_dir"] == "/DATA/Downloads/telegram"
    assert store.set_binding_download_dir(c, "u2", b["id"], "/x") is False   # unauthorized
