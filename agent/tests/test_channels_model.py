# NimoOS-AI/agent/tests/test_channels_model.py
import pytest

from channels.model import (ChannelAdapter, ChannelCapabilities,
                            InboundMessage, OutboundMessage, split_text)


def test_split_text_short_passthrough():
    assert split_text("hello", 100) == ["hello"]
    assert split_text("", 100) == []


def test_split_text_prefers_newline_boundary():
    text = "a" * 60 + "\n" + "b" * 60
    chunks = split_text(text, 100)
    assert chunks == ["a" * 60, "b" * 60]


def test_split_text_hard_cut_when_no_boundary():
    chunks = split_text("x" * 250, 100)
    assert [len(c) for c in chunks] == [100, 100, 50]
    assert "".join(chunks) == "x" * 250


def test_adapter_abc_requires_core_methods():
    import pytest
    with pytest.raises(TypeError):
        ChannelAdapter("i1", {}, None)  # abstract


def test_inbound_defaults():
    m = InboundMessage(channel_type="t", instance_id="i", external_chat_id="c",
                       external_user_id="u", external_username=None,
                       message_id="1", text="hi")
    assert m.reply_to is None and m.raw == {}
    assert OutboundMessage(text="ok").text == "ok"
    caps = ChannelCapabilities(max_text_len=10)
    assert caps.supports_edit is False and caps.supports_typing is False


def test_inbound_attachment_and_message_field():
    from channels.model import InboundAttachment, InboundMessage
    a = InboundAttachment(filename="x.png", mime="image/png", tmp_path="/tmp/x", size=12)
    assert (a.filename, a.mime, a.tmp_path, a.size) == ("x.png", "image/png", "/tmp/x", 12)
    m = InboundMessage(channel_type="telegram", instance_id="i", external_chat_id="c",
                       external_user_id="u", external_username=None, message_id="1", text="")
    assert m.attachments == []          # 默认空,不破坏 M1 构造
    m.attachments.append(a)
    assert m.attachments[0].size == 12


def test_channel_capabilities_supports_media_default_false():
    caps = ChannelCapabilities(max_text_len=10)
    assert caps.supports_media is False


@pytest.mark.asyncio
async def test_adapter_default_send_file_unsupported_returns_none(caplog):
    class DummyAdapter(ChannelAdapter):
        channel_type = "dummy"
        capabilities = ChannelCapabilities(max_text_len=10)

        async def start(self): ...
        async def stop(self): ...
        async def send(self, external_chat_id, msg): return None

    a = DummyAdapter("i1", {}, None)
    with caplog.at_level("WARNING"):
        result = await a.send_file("chat1", "/tmp/whatever.png")
    assert result is None
    assert any("send_file not supported by dummy" in rec.message
               for rec in caplog.records)


@pytest.mark.asyncio
async def test_channeladapter_button_defaults_and_on_callback(caplog):
    from channels.model import ChannelAdapter, ChannelCapabilities, OutboundMessage

    class Dummy(ChannelAdapter):
        channel_type = "dummy"
        capabilities = ChannelCapabilities(max_text_len=100)
        async def start(self): ...
        async def stop(self): ...
        async def send(self, external_chat_id, msg): return "1"

    seen = []
    async def on_cb(adapter, chat_id, data): seen.append((chat_id, data))
    d = Dummy("i", {}, on_inbound=None, on_callback=on_cb)
    assert d._on_callback is on_cb
    assert await d.send_buttons("c", "hi", [("ok", "cf:x:a")]) is None   # default: unsupported
    assert await d.edit_to_resolved("c", "5", "done") is None            # default: no-op
