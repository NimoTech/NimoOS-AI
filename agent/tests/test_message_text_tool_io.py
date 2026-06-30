import context_compaction as cc


def test_function_call_text_has_name_and_args():
    m = {"type": "function_call", "name": "list_dir",
         "arguments": '{"path": "/media/RAID/Image"}', "call_id": "c1"}
    txt = cc._message_text(m)
    assert "list_dir" in txt and "/media/RAID/Image" in txt
    assert cc.estimate_tokens(txt) > 0


def test_function_call_output_counted_full():
    big = '[' + ','.join('{"name": "f%d.mp4", "size": 123}' % i
                         for i in range(200)) + ']'
    m = {"type": "function_call_output", "call_id": "c1", "output": big}
    txt = cc._message_text(m)                 # full (no cap)
    assert "f0.mp4" in txt and "f199.mp4" in txt
    assert cc.estimate_tokens(txt) > 200


def test_function_call_output_truncated_for_summary():
    big = "x" * 8000
    m = {"type": "function_call_output", "output": big}
    full = cc._message_text(m)
    capped = cc._message_text(m, max_output_chars=500)
    assert len(full) >= 8000
    assert len(capped) < 700              # ~500 + marker
    assert "…[+" in capped                # truncation marker present


def test_truncation_only_affects_output_items():
    # non-output items unaffected by max_output_chars
    fc = {"type": "function_call", "name": "n", "arguments": "a" * 2000}
    assert cc._message_text(fc, max_output_chars=500) == cc._message_text(fc)
    msg = {"role": "user", "content": "u" * 2000}
    assert cc._message_text(msg, max_output_chars=500) == cc._message_text(msg)


def test_function_call_output_non_str():
    m = {"type": "function_call_output", "output": [{"name": "a"}, {"name": "b"}]}
    txt = cc._message_text(m)
    assert "a" in txt and "b" in txt      # json-serialized, no crash


def test_reasoning_summary_list_and_str():
    m = {"type": "reasoning",
         "summary": [{"text": "user said hello, reply zh", "type": "summary_text"}]}
    assert "hello" in cc._message_text(m) and cc.estimate_tokens(cc._message_text(m)) > 0
    assert "plain" in cc._message_text({"type": "reasoning", "summary": "plain str"})


def test_standard_message_unchanged():
    assert cc._message_text({"role": "user", "content": "hello"}) == "user: hello"
    m = {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "你好"}]}
    assert "你好" in cc._message_text(m)


def test_unknown_item_returns_empty():
    assert cc._message_text({"type": "weird", "id": "x", "provider_data": {"k": 1}}) == ""
    assert cc._message_text({"id": "x"}) == ""


def test_estimate_counts_tool_output_far_above_content_only():
    big_out = "x" * 8000
    history = [
        {"role": "user", "content": "看看目录"},
        {"type": "function_call", "name": "list_dir", "arguments": "{}"},
        {"type": "function_call_output", "output": big_out},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "好的"}]},
    ]
    assert cc.estimate_messages_tokens(history) > 1500   # ~8000/4*1.15
