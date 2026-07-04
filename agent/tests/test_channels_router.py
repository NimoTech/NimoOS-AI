# NimoOS-AI/agent/tests/test_channels_router.py
import asyncio
import time

import pytest
import db as db_module
from channels import store
from channels.model import ChannelCapabilities, InboundMessage
from channels.router import ChannelRouter, MSG_BUSY


class FakeAdapter:
    channel_type = "telegram"

    def __init__(self, max_len=200):
        self.capabilities = ChannelCapabilities(max_text_len=max_len,
                                                supports_typing=True)
        self.sent, self.typing = [], 0

    async def send(self, chat_id, msg):
        self.sent.append((chat_id, msg.text))
        return "1"

    async def send_typing(self, chat_id):
        self.typing += 1


class FakeSink:
    def __init__(self, events):
        self._events = list(events)

    def subscribe(self):
        return list(self._events), asyncio.Queue()

    def unsubscribe(self, q):
        pass


def _msg(text, chat="c1", user="tg1", instance="i1"):
    return InboundMessage(channel_type="telegram", instance_id=instance,
                          external_chat_id=chat, external_user_id=user,
                          external_username="alice", message_id="m1", text=text)


@pytest.fixture
def env(tmp_path):
    conn = db_module.init_db(str(tmp_path / "t.db"),
                             snapshots_root=str(tmp_path / "snaps"))
    inst = store.create_instance(conn, "telegram", "", {"bot_token": "t"},
                                 "u1", 0)
    calls = {"runs": [], "cancels": []}

    def start_run(session_id, user_id, message, creds, chat_username):
        calls["runs"].append((session_id, user_id, message, creds))
        return FakeSink([{"type": "message", "content": "pong " + message},
                         {"type": "done"}])

    async def cancel_run(session_id):
        calls["cancels"].append(session_id)
        return True

    async def resolve_credentials(user_id, model):
        if model == "broken":
            return None
        return {"provider_type": "ollama", "base_url": "http://x/v1",
                "api_key": "ollama", "model": model}

    router = ChannelRouter(conn, start_run=start_run, cancel_run=cancel_run,
                           resolve_credentials=resolve_credentials)
    return conn, inst, router, calls


@pytest.mark.asyncio
async def test_unpaired_message_gets_single_rate_limited_hint(env):
    conn, inst, router, _ = env
    a = FakeAdapter()
    await router.handle(a, _msg("hi", instance=inst["id"]))
    await router.handle(a, _msg("hi again", instance=inst["id"]))
    assert len(a.sent) == 1  # second hint suppressed by rate limit
    assert "配对" in a.sent[0][1]


@pytest.mark.asyncio
async def test_pair_flow_then_run_roundtrip(env):
    conn, inst, router, calls = env
    a = FakeAdapter()
    code, _ = store.create_pairing_code(conn, inst["id"], "u1",
                                        now_ms=int(time.time() * 1000))
    await router.handle(a, _msg(f"/pair {code}", instance=inst["id"]))
    assert store.get_binding(conn, inst["id"], "tg1")["user_id"] == "u1"
    # no default model yet -> guidance, no run
    await router.handle(a, _msg("hello", instance=inst["id"]))
    assert calls["runs"] == []
    b = store.get_binding(conn, inst["id"], "tg1")
    store.set_binding_model(conn, "u1", b["id"], "qwen3")
    await router.handle(a, _msg("hello", instance=inst["id"]))
    assert len(calls["runs"]) == 1
    sid, uid, message, creds = calls["runs"][0]
    assert uid == "u1" and message == "hello" and creds["model"] == "qwen3"
    assert a.sent[-1][1] == "pong hello"
    assert a.typing >= 1
    # chat->session mapping persisted and reused
    chat = store.get_chat(conn, inst["id"], "c1")
    assert chat["session_id"] == sid
    await router.handle(a, _msg("again", instance=inst["id"]))
    assert calls["runs"][1][0] == sid


@pytest.mark.asyncio
async def test_pair_bad_code_limit_then_silent(env):
    conn, inst, router, _ = env
    a = FakeAdapter()
    for _i in range(7):
        await router.handle(a, _msg("/pair 00000000", instance=inst["id"]))
    # 5 error replies allowed per window, then silence
    assert len(a.sent) == 5


@pytest.mark.asyncio
async def test_new_stop_whoami_commands(env):
    conn, inst, router, calls = env
    a = FakeAdapter()
    code, _ = store.create_pairing_code(conn, inst["id"], "u1",
                                        now_ms=int(time.time() * 1000))
    await router.handle(a, _msg(f"/pair {code}", instance=inst["id"]))
    b = store.get_binding(conn, inst["id"], "tg1")
    store.set_binding_model(conn, "u1", b["id"], "qwen3")
    await router.handle(a, _msg("hello", instance=inst["id"]))
    sid1 = store.get_chat(conn, inst["id"], "c1")["session_id"]
    await router.handle(a, _msg("/new", instance=inst["id"]))
    await router.handle(a, _msg("hello2", instance=inst["id"]))
    sid2 = store.get_chat(conn, inst["id"], "c1")["session_id"]
    assert sid1 != sid2
    await router.handle(a, _msg("/stop", instance=inst["id"]))
    assert calls["cancels"] == [sid2]
    await router.handle(a, _msg("/whoami", instance=inst["id"]))
    assert "u1" in a.sent[-1][1] and "qwen3" in a.sent[-1][1]


@pytest.mark.asyncio
async def test_long_reply_is_chunked(env):
    conn, inst, router, calls = env
    a = FakeAdapter(60)  # max_text_len=60
    code, _ = store.create_pairing_code(conn, inst["id"], "u1",
                                        now_ms=int(time.time() * 1000))
    await router.handle(a, _msg(f"/pair {code}", instance=inst["id"]))
    b = store.get_binding(conn, inst["id"], "tg1")
    store.set_binding_model(conn, "u1", b["id"], "qwen3")
    await router.handle(a, _msg("x" * 100, instance=inst["id"]))
    # reply is "pong " + 100 x's = 105 chars -> 2 chunks under limit 60
    assert all(len(t) <= 60 for _c, t in a.sent)
    assert sum(len(t) for _c, t in a.sent[-2:]) >= 100


@pytest.mark.asyncio
async def test_credentials_failure_reports_error(env):
    conn, inst, router, calls = env
    a = FakeAdapter()
    code, _ = store.create_pairing_code(conn, inst["id"], "u1",
                                        now_ms=int(time.time() * 1000))
    await router.handle(a, _msg(f"/pair {code}", instance=inst["id"]))
    b = store.get_binding(conn, inst["id"], "tg1")
    store.set_binding_model(conn, "u1", b["id"], "broken")
    await router.handle(a, _msg("hello", instance=inst["id"]))
    assert calls["runs"] == []
    assert "模型" in a.sent[-1][1] or "provider" in a.sent[-1][1]


class GatedSink:
    """start_run sink whose events are released by an external gate."""

    def __init__(self, gate, label):
        self._gate = gate
        self._label = label

    def subscribe(self):
        q = asyncio.Queue()

        async def feed():
            await self._gate.wait()
            q.put_nowait({"type": "message", "content": "pong " + self._label})
            q.put_nowait({"type": "done"})

        asyncio.ensure_future(feed())
        return [], q

    def unsubscribe(self, q):
        pass


def _gated_router(conn, gate, started):
    def start_run(session_id, user_id, message, creds, chat_username):
        started.append(message)
        return GatedSink(gate, message)

    async def cancel_run(session_id):
        return True

    async def resolve_credentials(user_id, model):
        return {"provider_type": "ollama", "base_url": "http://x/v1",
                "api_key": "o", "model": model}

    return ChannelRouter(conn, start_run=start_run, cancel_run=cancel_run,
                         resolve_credentials=resolve_credentials)


async def _paired(conn, inst, router, adapter):
    code, _ = store.create_pairing_code(conn, inst["id"], "u1",
                                        now_ms=int(time.time() * 1000))
    await router.handle(adapter, _msg(f"/pair {code}", instance=inst["id"]))
    b = store.get_binding(conn, inst["id"], "tg1")
    store.set_binding_model(conn, "u1", b["id"], "qwen3")


@pytest.mark.asyncio
async def test_same_chat_serializes_fifo_and_rejects_over_pending(tmp_path):
    conn = db_module.init_db(str(tmp_path / "t.db"),
                             snapshots_root=str(tmp_path / "snaps"))
    inst = store.create_instance(conn, "telegram", "", {"bot_token": "t"},
                                 "u1", 0)
    gate, started = asyncio.Event(), []
    router = _gated_router(conn, gate, started)
    a = FakeAdapter()
    await _paired(conn, inst, router, a)

    tasks = [asyncio.create_task(
        router.handle(a, _msg(f"m{i}", instance=inst["id"])))
        for i in range(1, 6)]
    await asyncio.sleep(0.05)
    assert started == ["m1"]                       # only head of queue runs
    assert [t for _c, t in a.sent].count(MSG_BUSY) == 2   # m4, m5 rejected
    gate.set()
    await asyncio.gather(*tasks)
    assert started == ["m1", "m2", "m3"]           # FIFO order preserved


@pytest.mark.asyncio
async def test_different_chats_run_concurrently(tmp_path):
    conn = db_module.init_db(str(tmp_path / "t.db"),
                             snapshots_root=str(tmp_path / "snaps"))
    inst = store.create_instance(conn, "telegram", "", {"bot_token": "t"},
                                 "u1", 0)
    gate, started = asyncio.Event(), []
    router = _gated_router(conn, gate, started)
    a = FakeAdapter()
    await _paired(conn, inst, router, a)

    tasks = [asyncio.create_task(
        router.handle(a, _msg("m1", chat="c1", instance=inst["id"]))),
        asyncio.create_task(
        router.handle(a, _msg("m2", chat="c2", instance=inst["id"])))]
    await asyncio.sleep(0.05)
    assert started == ["m1", "m2"]     # both in-flight before gate: overlap
    gate.set()
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_stranger_tracking_dicts_are_bounded(env, monkeypatch):
    conn, inst, router, _ = env
    import channels.router as router_mod
    monkeypatch.setattr(router_mod, "MAX_TRACKED_KEYS", 10)
    a = FakeAdapter()
    for i in range(50):
        await router.handle(a, _msg("hi", user=f"tg{i}", chat=f"c{i}",
                                    instance=inst["id"]))
    assert len(router._unpaired_last) <= 10
