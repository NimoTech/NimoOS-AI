import asyncio
import json
import httpx
import pytest
from channels.telegram import TelegramAdapter


def _update(uid=1, chat_type="private", text="hello", chat_id=99, from_id=7):
    return {"update_id": uid,
            "message": {"message_id": 5, "text": text,
                        "chat": {"id": chat_id, "type": chat_type},
                        "from": {"id": from_id, "username": "alice"}}}


def _bot_api(updates_holder, sent):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/getUpdates"):
            batch, updates_holder[:] = list(updates_holder), []
            return httpx.Response(200, json={"ok": True, "result": batch})
        if path.endswith("/sendMessage"):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True,
                                             "result": {"message_id": 42}})
        if path.endswith("/sendChatAction"):
            sent.append({"action": "typing"})
            return httpx.Response(200, json={"ok": True, "result": True})
        if path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True,
                                             "result": {"username": "nimo_bot"}})
        return httpx.Response(404, json={"ok": False})
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_poll_once_dispatches_private_text_only():
    got = []

    async def on_inbound(adapter, msg):
        got.append(msg)

    updates = [_update(uid=1), _update(uid=2, chat_type="group"),
               _update(uid=3, text=None)]
    a = TelegramAdapter("i1", {"bot_token": "123:abc"}, on_inbound,
                        transport=_bot_api(updates, []))
    await a._poll_once()
    await asyncio.gather(*a._inflight)
    assert len(got) == 1
    m = got[0]
    assert (m.channel_type, m.instance_id, m.external_chat_id,
            m.external_user_id, m.external_username, m.text) == \
           ("telegram", "i1", "99", "7", "alice", "hello")
    assert a._offset == 4  # max(update_id)+1, non-dispatched updates ack'd too
    await a.stop()


@pytest.mark.asyncio
async def test_send_and_typing_and_validate():
    from channels.model import OutboundMessage
    sent = []
    a = TelegramAdapter("i1", {"bot_token": "123:abc"}, None,
                        transport=_bot_api([], sent))
    mid = await a.send("99", OutboundMessage(text="yo"))
    assert mid == "42" and sent[0]["chat_id"] == "99" and sent[0]["text"] == "yo"
    await a.send_typing("99")
    assert sent[-1] == {"action": "typing"}
    info = await TelegramAdapter.validate_token("123:abc",
                                                transport=_bot_api([], []))
    assert info == {"bot_username": "nimo_bot"}
    await a.stop()


@pytest.mark.asyncio
async def test_validate_token_rejects_bad_token():
    bad = httpx.MockTransport(
        lambda r: httpx.Response(401, json={"ok": False}))
    assert await TelegramAdapter.validate_token("bad", transport=bad) is None
