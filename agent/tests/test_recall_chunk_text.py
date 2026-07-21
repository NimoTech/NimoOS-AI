from recall_index import chunk_messages, _msg_text


def test_block_list_extracts_clean_text():
    msgs = [{"role": "assistant", "content": [
        {"type": "output_text", "text": "hello world", "annotations": []},
        {"type": "output_text", "text": "second block", "logprobs": []},
    ]}]
    chunks = chunk_messages(msgs, start_chunk_no=0, now=1)
    assert chunks[0]["text"] == "assistant: hello world\nsecond block"
    assert "annotations" not in chunks[0]["text"]


def test_str_content_passthrough():
    chunks = chunk_messages([{"role": "user", "content": "plain"}],
                            start_chunk_no=0, now=1)
    assert chunks[0]["text"] == "user: plain"


def test_none_and_tool_items_skipped():
    msgs = [
        {"type": "reasoning", "content": None},
        {"type": "function_call", "name": "t", "arguments": "{}"},
        {"type": "function_call_output", "output": "big blob"},
        {"role": "assistant", "content": [{"type": "output_text", "text": ""}]},
    ]
    assert chunk_messages(msgs, start_chunk_no=0, now=1) == []


def test_msg_text_shapes():
    assert _msg_text({"content": "s"}) == "s"
    assert _msg_text({"content": None}) == ""
    assert _msg_text({"content": {"weird": 1}}) == ""
    assert _msg_text({}) == ""
