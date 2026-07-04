import asyncio
import types
import httpx
import pytest
from channels.discord import DiscordAdapter
from channels.model import OutboundMessage


def _msg(content="hello", *, dm=True, from_bot=False, author_id=7,
         author_name="alice", chan_id=99, msg_id=5):
    """Duck-typed stand-in for a discord.Message (no discord import needed)."""
    channel = types.SimpleNamespace(id=chan_id, is_dm=dm)
    author = types.SimpleNamespace(id=author_id, name=author_name, bot=from_bot)
    return types.SimpleNamespace(content=content, channel=channel,
                                 author=author, id=msg_id)


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
