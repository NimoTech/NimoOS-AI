# NimoOS-AI/agent/tests/test_channels_manager.py
import pytest
import db as db_module
from channels import store
from channels.manager import ChannelManager
from channels.model import ChannelAdapter, ChannelCapabilities


class RecordingAdapter(ChannelAdapter):
    channel_type = "telegram"
    capabilities = ChannelCapabilities(max_text_len=10)
    events: list = []

    def __init__(self, instance_id, config, on_inbound):
        super().__init__(instance_id, config, on_inbound)
        RecordingAdapter.events.append(("init", instance_id, config))

    async def start(self):
        RecordingAdapter.events.append(("start", self.instance_id))

    async def stop(self):
        RecordingAdapter.events.append(("stop", self.instance_id))

    async def send(self, external_chat_id, msg):
        return None


class FakeRouter:
    async def handle(self, adapter, msg):
        pass


@pytest.fixture
def conn(tmp_path):
    RecordingAdapter.events = []
    return db_module.init_db(str(tmp_path / "t.db"),
                             snapshots_root=str(tmp_path / "snaps"))


@pytest.mark.asyncio
async def test_start_reload_stop_lifecycle(conn):
    mgr = ChannelManager(conn, FakeRouter(),
                         adapters={"telegram": RecordingAdapter})
    inst = store.create_instance(conn, "telegram", "", {"bot_token": "t1"},
                                 "u1", 0)
    await mgr.start_all()
    assert ("start", inst["id"]) in RecordingAdapter.events
    # disable -> stopped on reload
    store.set_instance_enabled(conn, inst["id"], False, 1)
    await mgr.reload()
    assert ("stop", inst["id"]) in RecordingAdapter.events
    # re-enable with changed config -> fresh adapter gets new token
    store.set_instance_enabled(conn, inst["id"], True, 2)
    conn.execute("UPDATE channel_instances SET config_json=? WHERE id=?",
                 ('{"bot_token": "t2"}', inst["id"]))
    conn.commit()
    await mgr.reload()
    assert ("init", inst["id"], {"bot_token": "t2"}) in RecordingAdapter.events
    await mgr.stop_all()
    assert RecordingAdapter.events.count(("stop", inst["id"])) == 2


@pytest.mark.asyncio
async def test_unknown_channel_type_ignored(conn):
    store.create_instance(conn, "martian", "", {}, "u1", 0)
    mgr = ChannelManager(conn, FakeRouter(),
                         adapters={"telegram": RecordingAdapter})
    await mgr.start_all()   # must not raise
    assert RecordingAdapter.events == []
