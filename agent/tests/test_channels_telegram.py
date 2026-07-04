# NimoOS-AI/agent/tests/test_channels_telegram.py
import asyncio
import json
import os
import httpx
import pytest
from channels import telegram as telegram_module
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


def _bot_api_with_file(updates_holder):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/getUpdates"):
            batch, updates_holder[:] = list(updates_holder), []
            return httpx.Response(200, json={"ok": True, "result": batch})
        if path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True,
                                             "result": {"file_path": "docs/f.txt"}})
        if "/file/" in path:
            return httpx.Response(200, content=b"abc")
        return httpx.Response(404, json={"ok": False})
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_poll_extracts_document_attachment():
    got = []

    async def on_inbound(adapter, msg):
        got.append(msg)

    upd = {"update_id": 1, "message": {"message_id": 5,
        "chat": {"id": 99, "type": "private"}, "from": {"id": 7, "username": "a"},
        "document": {"file_id": "FID", "file_name": "f.txt", "file_size": 3,
                     "mime_type": "text/plain"}}}
    a = TelegramAdapter("i1", {"bot_token": "123:abc"}, on_inbound,
                        transport=_bot_api_with_file([upd]))
    await a._poll_once()
    await asyncio.gather(*a._inflight)
    assert len(got) == 1 and len(got[0].attachments) == 1
    att = got[0].attachments[0]
    assert att.filename == "f.txt" and os.path.exists(att.tmp_path)
    assert att.mime == "text/plain" and att.size == 3
    await a.stop()


@pytest.mark.asyncio
async def test_poll_uses_caption_when_text_missing():
    """A photo sent with a caption (no `text` field) must still carry the
    user's instruction through as InboundMessage.text, plus its attachment."""
    got = []

    async def on_inbound(adapter, msg):
        got.append(msg)

    upd = {"update_id": 1, "message": {"message_id": 5,
        "chat": {"id": 99, "type": "private"}, "from": {"id": 7, "username": "a"},
        "caption": "summarize this receipt",
        "photo": [{"file_id": "FID", "file_unique_id": "u1"}]}}
    a = TelegramAdapter("i1", {"bot_token": "123:abc"}, on_inbound,
                        transport=_bot_api_with_file([upd]))
    await a._poll_once()
    await asyncio.gather(*a._inflight)
    assert len(got) == 1
    assert got[0].text == "summarize this receipt"
    assert len(got[0].attachments) == 1
    await a.stop()


def _bot_api_getfile_fails(updates_holder):
    """Mock transport whose getFile call reports failure, simulating a
    single-attachment download error while getUpdates still works."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/getUpdates"):
            batch, updates_holder[:] = list(updates_holder), []
            return httpx.Response(200, json={"ok": True, "result": batch})
        if path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": False, "error_code": 400,
                                             "description": "file not found"})
        return httpx.Response(404, json={"ok": False})
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_poll_download_failure_keeps_text_drops_attachment():
    """A single attachment's download failure must be isolated: the message
    is still dispatched with its text intact and zero attachments, rather
    than being dropped entirely."""
    got = []

    async def on_inbound(adapter, msg):
        got.append(msg)

    upd = {"update_id": 1, "message": {"message_id": 5,
        "text": "please check this",
        "chat": {"id": 99, "type": "private"}, "from": {"id": 7, "username": "a"},
        "document": {"file_id": "FID", "file_name": "f.txt",
                     "mime_type": "text/plain"}}}
    a = TelegramAdapter("i1", {"bot_token": "123:abc"}, on_inbound,
                        transport=_bot_api_getfile_fails([upd]))
    await a._poll_once()
    await asyncio.gather(*a._inflight)
    assert len(got) == 1
    assert got[0].text == "please check this"
    assert got[0].attachments == []
    await a.stop()


@pytest.mark.asyncio
async def test_poll_document_over_cap_dropped_and_cleaned_up(monkeypatch):
    """A document whose download exceeds the size cap must be skipped (zero
    attachments, message still dispatched) and its temp file removed rather
    than leaked on disk. A tiny injected cap stands in for the real 20MB
    limit so the test doesn't need to transfer a huge payload."""
    got = []

    async def on_inbound(adapter, msg):
        got.append(msg)

    upd = {"update_id": 1, "message": {"message_id": 5,
        "text": "here's a big file",
        "chat": {"id": 99, "type": "private"}, "from": {"id": 7, "username": "a"},
        "document": {"file_id": "FID", "file_name": "big.bin", "file_size": 100,
                     "mime_type": "application/octet-stream"}}}
    a = TelegramAdapter("i1", {"bot_token": "123:abc"}, on_inbound,
                        transport=_bot_api_with_file([upd]))

    # Inject a 1-byte cap so the mock file body ("abc", 3 bytes) trips the
    # existing cap-abort path in _download_tg_file without touching the
    # real default (20MB) or the download URL scheme.
    async def tiny_cap_download(file_id):
        return await TelegramAdapter._download_tg_file(a, file_id, max_file=1)
    a._download_tg_file = tiny_cap_download

    created_paths = []
    real_ntf = telegram_module.tempfile.NamedTemporaryFile

    def spy_ntf(*args, **kwargs):
        f = real_ntf(*args, **kwargs)
        created_paths.append(f.name)
        return f
    monkeypatch.setattr(telegram_module.tempfile, "NamedTemporaryFile", spy_ntf)

    await a._poll_once()
    await asyncio.gather(*a._inflight)
    assert len(got) == 1
    assert got[0].attachments == []
    assert len(created_paths) == 1
    assert not os.path.exists(created_paths[0])
    await a.stop()


def _bot_api_send_file(method_calls, requests_holder=None):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for method in ("sendPhoto", "sendVideo", "sendAudio", "sendDocument"):
            if path.endswith(f"/{method}"):
                method_calls.append(method)
                if requests_holder is not None:
                    requests_holder.append(request)
                return httpx.Response(200, json={"ok": True,
                                                 "result": {"message_id": 123}})
        return httpx.Response(404, json={"ok": False})
    return httpx.MockTransport(handler)


def _multipart_field(request: httpx.Request, name: str) -> str | None:
    """Pull a plain (non-file) multipart field's decoded value out of a
    captured httpx.Request body, so tests can assert on what was actually
    sent rather than trusting the mock to have received it."""
    boundary = request.headers["content-type"].split("boundary=")[1]
    parts = request.content.split(f"--{boundary}".encode())
    marker = f'name="{name}"'.encode()
    for part in parts:
        if marker in part:
            return part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0].decode()
    return None


@pytest.mark.asyncio
async def test_send_file_image_uses_send_photo(tmp_path):
    p = tmp_path / "pic.png"
    p.write_bytes(b"fakepng")
    calls = []
    requests = []
    a = TelegramAdapter("i1", {"bot_token": "123:abc"}, None,
                        transport=_bot_api_send_file(calls, requests))
    mid = await a.send_file("99", str(p), caption="look")
    assert calls == ["sendPhoto"]
    assert mid == "123"
    assert _multipart_field(requests[0], "chat_id") == "99"
    assert _multipart_field(requests[0], "caption") == "look"
    await a.stop()


@pytest.mark.asyncio
async def test_send_file_unknown_extension_with_octet_stream_mime_uses_send_document(tmp_path):
    """`.bin` guesses to ("application/octet-stream", None) — a known mime
    with an unmapped prefix. This is distinct from a truly unrecognized
    extension (see test below), which guesses to (None, None)."""
    p = tmp_path / "data.bin"
    p.write_bytes(b"\x00\x01\x02")
    calls = []
    a = TelegramAdapter("i1", {"bot_token": "123:abc"}, None,
                        transport=_bot_api_send_file(calls))
    mid = await a.send_file("99", str(p))
    assert calls == ["sendDocument"]
    assert mid == "123"
    await a.stop()


@pytest.mark.asyncio
async def test_send_file_unrecognized_extension_uses_send_document(tmp_path):
    """mimetypes.guess_type('data.xyz123') == (None, None) — the true
    "no guess at all" path, as opposed to a recognized-but-unmapped mime
    like application/octet-stream. Both must fall through to sendDocument."""
    p = tmp_path / "data.xyz123"
    p.write_bytes(b"\x00\x01\x02")
    calls = []
    a = TelegramAdapter("i1", {"bot_token": "123:abc"}, None,
                        transport=_bot_api_send_file(calls))
    mid = await a.send_file("99", str(p))
    assert calls == ["sendDocument"]
    assert mid == "123"
    await a.stop()


@pytest.mark.asyncio
async def test_send_file_failure_raises_with_reason(tmp_path):
    """A Telegram-reported failure ({"ok": false, ...}) must raise so the
    caller can distinguish a real send failure (relay the reason to the
    model) from `None`, which means "unsupported by this channel"."""
    p = tmp_path / "data.bin"
    p.write_bytes(b"\x00")
    bad = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"ok": False,
                                            "description": "chat not found"}))
    a = TelegramAdapter("i1", {"bot_token": "123:abc"}, None, transport=bad)
    with pytest.raises(RuntimeError, match="chat not found"):
        await a.send_file("99", str(p))
    await a.stop()


@pytest.mark.asyncio
async def test_send_file_http_error_raises(tmp_path):
    """A non-2xx HTTP response (e.g. network/proxy failure) must not be
    mistaken for a successful send."""
    p = tmp_path / "data.bin"
    p.write_bytes(b"\x00")
    bad = httpx.MockTransport(lambda r: httpx.Response(502, text="bad gateway"))
    a = TelegramAdapter("i1", {"bot_token": "123:abc"}, None, transport=bad)
    with pytest.raises(httpx.HTTPStatusError):
        await a.send_file("99", str(p))
    await a.stop()


def test_telegram_capabilities_support_media():
    assert TelegramAdapter.capabilities.supports_media is True
