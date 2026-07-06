import asyncio
import pytest
from channels.driver import ChannelRunDriver


class FakeSink:
    def __init__(self, past=(), live=()):
        self._past = list(past)
        self._live = list(live)
        self.unsubscribed = False

    def subscribe(self):
        q = asyncio.Queue()
        for ev in self._live:
            q.put_nowait(ev)
        return list(self._past), q

    def unsubscribe(self, q):
        self.unsubscribed = True


def _driver(sent, confirms=None):
    async def send_text(text):
        sent.append(text)
    async def surface(ev):
        (confirms if confirms is not None else []).append(ev)
    async def no_sleep(_):
        return None
    return ChannelRunDriver(send_text=send_text,
                            surface_confirm=surface if confirms is not None else None,
                            sleep=no_sleep, now=lambda: 0.0)


@pytest.mark.asyncio
async def test_flushes_conclusion_per_toolcall_boundary():
    sent = []
    sink = FakeSink(past=[
        {"type": "message_delta", "content": "Let me check. "},
        {"type": "message_delta", "content": "Looking now."},
        {"type": "tool_call"},
        {"type": "message_delta", "content": "Found it: 42."},
        {"type": "done"}])
    await _driver(sent).drive(sink)
    assert sent == ["Let me check. Looking now.", "Found it: 42."]
    assert sink.unsubscribed is True


@pytest.mark.asyncio
async def test_message_event_overrides_buffer_and_empty_rounds_skipped():
    sent = []
    sink = FakeSink(past=[
        {"type": "tool_call"},                       # no text yet -> no empty send
        {"type": "message", "content": "final answer"},
        {"type": "done"}])
    await _driver(sent).drive(sink)
    assert sent == ["final answer"]


@pytest.mark.asyncio
async def test_error_surfaced_and_empty_reply_fallback():
    sent = []
    sink = FakeSink(past=[{"type": "error", "content": "boom"}, {"type": "done"}])
    await _driver(sent).drive(sink)
    assert sent == ["出错了 (error): boom"]

    sent2 = []
    sink2 = FakeSink(past=[{"type": "done"}])         # nothing at all
    await _driver(sent2).drive(sink2)
    assert sent2 == ["(无回复 / empty reply)"]


@pytest.mark.asyncio
async def test_confirm_event_flushes_then_forwards():
    sent, confirms = [], []
    sink = FakeSink(past=[
        {"type": "message_delta", "content": "I need a file."},
        {"type": "access_request", "confirm_id": "c1", "path": "/DATA/x", "reason": "读取"},
        {"type": "done"}])
    await _driver(sent, confirms).drive(sink)
    assert sent == ["I need a file."]                 # conclusion flushed before the ask
    assert confirms == [{"type": "access_request", "confirm_id": "c1",
                         "path": "/DATA/x", "reason": "读取"}]
