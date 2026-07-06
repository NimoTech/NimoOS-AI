import asyncio
import pytest
from channels.collector import collect_final


class FakeSink:
    """Mimics RunSink.subscribe/unsubscribe for already-finished runs and
    live queues."""
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


@pytest.mark.asyncio
async def test_collects_from_past_events():
    sink = FakeSink(past=[{"type": "message", "content": "hi"},
                          {"type": "done"}])
    assert await collect_final(sink) == ("hi", None)
    assert sink.unsubscribed is True


@pytest.mark.asyncio
async def test_collects_from_live_queue_and_error():
    sink = FakeSink(live=[{"type": "message_delta", "content": "x"},
                          {"type": "error", "content": "boom"},
                          {"type": "done"}])
    final, error = await collect_final(sink)
    assert final == "x" and error == "boom"


@pytest.mark.asyncio
async def test_accumulates_streamed_message_deltas():
    # streaming models emit only deltas + done (no terminal 'message' event);
    # the reply must be reassembled from the deltas, not come back empty.
    sink = FakeSink(live=[{"type": "message_delta", "content": "Hel"},
                          {"type": "message_delta", "content": "lo"},
                          {"type": "message_delta", "content": " world"},
                          {"type": "done"}])
    assert await collect_final(sink) == ("Hello world", None)


@pytest.mark.asyncio
async def test_full_message_event_wins_over_deltas():
    # if a full 'message' event is present it is authoritative.
    sink = FakeSink(past=[{"type": "message_delta", "content": "partial"},
                          {"type": "message", "content": "complete"},
                          {"type": "done"}])
    assert await collect_final(sink) == ("complete", None)


@pytest.mark.asyncio
async def test_timeout_returns_timeout_error():
    sink = FakeSink()  # never emits done
    final, error = await collect_final(sink, timeout=0.05)
    assert error == "timeout"
