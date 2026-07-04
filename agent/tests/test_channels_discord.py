import asyncio
import os
import types
import httpx
import pytest
from channels.discord import DiscordAdapter
from channels.model import OutboundMessage


def _msg(content="hello", *, dm=True, from_bot=False, author_id=7,
         author_name="alice", chan_id=99, msg_id=5, attachments=None):
    """Duck-typed stand-in for a discord.Message (no discord import needed)."""
    channel = types.SimpleNamespace(id=chan_id, is_dm=dm)
    author = types.SimpleNamespace(id=author_id, name=author_name, bot=from_bot)
    return types.SimpleNamespace(content=content, channel=channel,
                                 author=author, id=msg_id,
                                 attachments=attachments if attachments is not None else [])


def _adapter(on_inbound=None, client=None, dm_check=None):
    a = DiscordAdapter("i1", {"bot_token": "t"}, on_inbound or (lambda *a: None),
                       client=client or object())
    # Inject the DM predicate so tests need no real discord.DMChannel.
    if dm_check is not None:
        a._is_dm = dm_check
    else:
        a._is_dm = lambda m: getattr(m.channel, "is_dm", False)
    a._is_self = lambda m: bool(getattr(m.author, "bot", False))
    return a


def test_to_inbound_maps_dm_text():
    a = _adapter()
    im = a._to_inbound(_msg(content="hi", chan_id=42, author_id=7,
                            author_name="bob", msg_id=88))
    assert (im.channel_type, im.instance_id, im.external_chat_id,
            im.external_user_id, im.external_username, im.message_id, im.text) == \
           ("discord", "i1", "42", "7", "bob", "88", "hi")


def test_to_inbound_skips_non_dm_and_self_and_empty():
    a = _adapter()
    assert a._to_inbound(_msg(dm=False)) is None          # guild message
    assert a._to_inbound(_msg(from_bot=True)) is None      # bot's own msg
    assert a._to_inbound(_msg(content="")) is None         # empty text


@pytest.mark.asyncio
async def test_handle_message_dispatches_inbound():
    got = []
    async def on_inbound(adapter, im):
        got.append(im)
    a = _adapter(on_inbound=on_inbound)
    await a._handle_message(_msg(content="yo", author_id=3))
    await asyncio.gather(*a._inflight)
    assert len(got) == 1 and got[0].text == "yo" and got[0].external_user_id == "3"


@pytest.mark.asyncio
async def test_send_resolves_channel_and_sends():
    sent = {}
    class FakeChannel:
        async def send(self, text): sent["text"] = text; return types.SimpleNamespace(id=555)
    class FakeClient:
        def get_channel(self, cid): return None
        async def fetch_channel(self, cid): sent["fetched"] = cid; return FakeChannel()
    a = _adapter(client=FakeClient())
    mid = await a.send("99", OutboundMessage(text="pong"))
    assert sent["text"] == "pong" and sent["fetched"] == 99 and mid == "555"


@pytest.mark.asyncio
async def test_discord_extracts_attachment():
    got = []
    async def on_inbound(adapter, msg): got.append(msg)
    class FakeAtt:
        filename = "f.txt"; content_type = "text/plain"; size = 3
        async def save(self, p): open(p, "wb").write(b"abc")
    msg = _msg(content="", attachments=[FakeAtt()])   # pure attachment, no text
    a = _adapter(on_inbound=on_inbound)
    await a._handle_message(msg)
    await asyncio.gather(*a._inflight)
    assert len(got) == 1 and len(got[0].attachments) == 1
    att = got[0].attachments[0]
    assert os.path.exists(att.tmp_path)
    assert (att.filename, att.mime, att.size) == ("f.txt", "text/plain", 3)


@pytest.mark.asyncio
async def test_discord_attachment_over_max_file_is_skipped():
    got = []
    async def on_inbound(adapter, msg): got.append(msg)
    class HugeAtt:
        filename = "big.bin"; content_type = "application/octet-stream"
        size = 21 * 1024 * 1024
        async def save(self, p): open(p, "wb").write(b"x")
    msg = _msg(content="hi", attachments=[HugeAtt()])
    a = _adapter(on_inbound=on_inbound)
    await a._handle_message(msg)
    await asyncio.gather(*a._inflight)
    assert len(got) == 1 and got[0].attachments == []


@pytest.mark.asyncio
async def test_discord_attachment_download_failure_skips_only_that_attachment():
    got = []
    async def on_inbound(adapter, msg): got.append(msg)
    class BadAtt:
        filename = "bad.bin"; content_type = "application/octet-stream"; size = 5
        async def save(self, p): raise RuntimeError("boom")
    msg = _msg(content="hi", attachments=[BadAtt()])
    a = _adapter(on_inbound=on_inbound)
    await a._handle_message(msg)
    await asyncio.gather(*a._inflight)
    assert len(got) == 1 and got[0].text == "hi" and got[0].attachments == []


@pytest.mark.asyncio
async def test_send_file_resolves_channel_and_sends_with_caption(tmp_path):
    p = tmp_path / "pic.png"
    p.write_bytes(b"fakepng")
    sent = {}
    class FakeChannel:
        async def send(self, content=None, file=None):
            sent["content"] = content
            sent["file"] = file
            return types.SimpleNamespace(id=777)
    class FakeClient:
        def get_channel(self, cid): return None
        async def fetch_channel(self, cid): sent["fetched"] = cid; return FakeChannel()
    a = _adapter(client=FakeClient())
    mid = await a.send_file("99", str(p), caption="look at this")
    assert sent["fetched"] == 99 and sent["content"] == "look at this"
    assert mid == "777"


@pytest.mark.asyncio
async def test_send_file_empty_caption_passes_none(tmp_path):
    p = tmp_path / "data.bin"
    p.write_bytes(b"\x00")
    sent = {}
    class FakeChannel:
        async def send(self, content=None, file=None):
            sent["content"] = content
            return types.SimpleNamespace(id=1)
    class FakeClient:
        def get_channel(self, cid): return FakeChannel()
    a = _adapter(client=FakeClient())
    await a.send_file("99", str(p))
    assert sent["content"] is None


def test_discord_capabilities_support_media():
    assert DiscordAdapter.capabilities.supports_media is True


@pytest.mark.asyncio
async def test_validate_token_ok_and_bad():
    def handler(request):
        assert request.headers.get("Authorization") == "Bot good"
        assert request.url.path.endswith("/users/@me")
        return httpx.Response(200, json={"id": "123456", "username": "nimo_bot"})
    ok = httpx.MockTransport(handler)
    info = await DiscordAdapter.validate_token("good", transport=ok)
    assert info == {"bot_username": "nimo_bot", "application_id": "123456"}
    bad = httpx.MockTransport(lambda r: httpx.Response(401, json={}))
    assert await DiscordAdapter.validate_token("bad", transport=bad) is None
