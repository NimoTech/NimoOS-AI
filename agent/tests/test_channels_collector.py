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
    assert final == "" and error == "boom"


@pytest.mark.asyncio
async def test_timeout_returns_timeout_error():
    sink = FakeSink()  # never emits done
    final, error = await collect_final(sink, timeout=0.05)
    assert error == "timeout"
