"""_convert_event delta classification: output-text deltas become message
content, reasoning deltas become thinking, and tool-call argument deltas are
dropped (they used to leak raw {"album_id": ...} JSON into the chat text)."""
from agents.stream_events import RawResponsesStreamEvent
from openai.types.responses import (
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseTextDeltaEvent,
)

from agent import _convert_event


def _wrap(data):
    return RawResponsesStreamEvent(data=data)


def test_output_text_delta_becomes_message_delta():
    ev = _wrap(ResponseTextDeltaEvent(
        content_index=0, delta="你有 6 个相册", item_id="i1", logprobs=[],
        output_index=0, sequence_number=1, type="response.output_text.delta"))
    state = {}
    out = _convert_event(ev, state=state)
    assert out == {"type": "message_delta", "content": "你有 6 个相册"}
    assert state["streamed_message"] is True


def test_function_call_arguments_delta_is_dropped():
    ev = _wrap(ResponseFunctionCallArgumentsDeltaEvent(
        delta='{"album_id": "acdfe851"}', item_id="i2",
        output_index=0, sequence_number=2,
        type="response.function_call_arguments.delta"))
    state = {}
    assert _convert_event(ev, state=state) is None
    assert "streamed_message" not in state
