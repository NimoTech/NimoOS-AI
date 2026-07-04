# NimoOS-AI/agent/tests/test_channels_model.py
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
