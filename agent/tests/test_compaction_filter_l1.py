import os

import compaction_filter as cf
import tool_output as to


def _fc(cid, name="web_fetch"): return {"type": "function_call", "call_id": cid, "name": name, "arguments": "{}"}
def _fo(cid, out): return {"type": "function_call_output", "call_id": cid, "output": out}
def _r(t, id_="r"): return {"type": "reasoning", "id": id_, "summary": [{"type": "summary_text", "text": t}]}
def _u(t): return {"role": "user", "content": t}


def _run(n, size):
    items = [_u("go")]
    for i in range(n):
        items += [_r("think %d" % i, f"r{i}"), _fc(f"c{i}"), _fo(f"c{i}", ("x" * size) + f"#{i}")]
    return items


def test_recent_outputs_untouched_old_ones_replaced(tmp_path):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    items = _run(12, 2000)
    new, n = cf.micro_compact(items, keep_recent_results=8, keep_chars=800)
    outs = [m for m in new if m.get("type") == "function_call_output"]
    assert n == 4
    assert all(len(m["output"]) > 1000 for m in outs[-8:])          # recent kept
    assert all("compacted: chars=2002" in m["output"] for m in outs[:4])
    assert all("path=" in m["output"] for m in outs[:4])
    assert (tmp_path / "c0.txt").read_text() == ("x" * 2000) + "#0"
    assert items[3]["output"].startswith("x" * 100)                  # original not mutated
    assert len(new) == len(items)


def test_small_old_outputs_are_kept():
    items = _run(12, 100)
    new, n = cf.micro_compact(items)
    outs = [m for m in new if m.get("type") == "function_call_output"]
    orig_outs = [m for m in items if m.get("type") == "function_call_output"]
    # n==0: no *outputs* replaced (they're all under keep_chars). Old reasoning
    # items still get stubbed per spec regardless of output size, so we only
    # assert on function_call_output items here, not the whole list.
    assert n == 0 and outs == orig_outs


def test_placeholder_keeps_trailer():
    to.OFFLOAD_DIR_VAR.set("")
    trailer = "[tool output offloaded: chars=9000 path=/DATA/x/call_1.txt]"
    ph = ("<untrusted-data source=\"tool-output-preview\">\n" + "p" * 1500 + "\n</untrusted-data>\n" + trailer + "\nUse read_file_lines...")
    items = [_u("go"), _fc("c0"), _fo("c0", ph)] + sum([[_fc(f"c{i}"), _fo(f"c{i}", "y" * 10)] for i in range(1, 10)], [])
    new, n = cf.micro_compact(items, keep_recent_results=8, keep_chars=800)
    out = [m for m in new if m.get("type") == "function_call_output"][0]["output"]
    assert n == 1 and trailer in out and len(out) < 600


def test_unstorable_output_gets_rerun_hint():
    to.OFFLOAD_DIR_VAR.set("")
    items = _run(10, 2000)
    new, n = cf.micro_compact(items, keep_recent_results=8, keep_chars=800)
    old = [m for m in new if m.get("type") == "function_call_output"][0]["output"]
    assert "re-run the tool" in old and "path=" not in old


def test_old_reasoning_compacted_synthetic_and_recent_kept():
    items = _run(12, 10)
    items.insert(1, _r("(no reasoning captured for this turn)", "__synthetic__"))
    new, _ = cf.micro_compact(items, keep_recent_results=8)
    rs = [m for m in new if m.get("type") == "reasoning"]
    assert rs[0]["id"] == "__synthetic__" and "no reasoning captured" in rs[0]["summary"][0]["text"]
    assert rs[1]["summary"][0]["text"] == "(reasoning compacted)"
    assert rs[-1]["summary"][0]["text"] == "think 11"
    assert items[2]["summary"][0]["text"] == "think 0"               # original untouched


def test_non_str_outputs_left_alone():
    items = [_u("go")] + sum([[_fc(f"c{i}"), _fo(f"c{i}", [{"type": "input_image", "image_url": "d" * 5000}])] for i in range(10)], [])
    new, n = cf.micro_compact(items)
    assert n == 0


def test_truncate_turns_keeps_first_user_and_last_turns():
    items = _run(10, 10)
    out = cf.truncate_turns(items, keep_turns=2)
    assert out[0] == items[0]
    assert [m.get("call_id") for m in out if m.get("type") == "function_call"] == ["c8", "c9"]


def test_estimate_since():
    items = _run(3, 100)
    assert cf.estimate_since(items, 0) > cf.estimate_since(items, 5) >= 0


def test_recent_boundary_keeps_whole_recent_turns():
    items = _run(12, 2000)                    # [user, (r,fc,fo) x 12]
    new, _ = cf.micro_compact(items, keep_recent_results=8, keep_chars=800)
    rs = [m for m in new if m.get("type") == "reasoning"]
    outs = [m for m in new if m.get("type") == "function_call_output"]
    # the 8 kept outputs are turns 4..11; their reasoning must be intact too
    assert [r["summary"][0]["text"] for r in rs[4:]] == [f"think {i}" for i in range(4, 12)]
    assert all(r["summary"][0]["text"] == "(reasoning compacted)" for r in rs[:4])
    assert all(len(o["output"]) > 1000 for o in outs[4:]) and all("compacted" in o["output"] for o in outs[:4])


def test_offload_file_not_rewritten_on_second_compact(tmp_path):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    items = _run(12, 2000)
    cf.micro_compact(items, keep_recent_results=8, keep_chars=800)
    path = tmp_path / "c0.txt"
    assert path.is_file()
    os.utime(path, (1, 1))
    new, n = cf.micro_compact(items, keep_recent_results=8, keep_chars=800)
    assert os.stat(path).st_mtime == 1                # file not rewritten
    out = [m for m in new if m.get("type") == "function_call_output"][0]["output"]
    assert n == 4 and "path=" in out


def test_recent_boundary_with_few_outputs_keeps_everything():
    # F1 regression: with fewer than keep_recent_results outputs, nothing is
    # old — everything the model just produced must survive untouched.
    items = _run(3, 5000)
    new, n = cf.micro_compact(items)          # default keep_recent_results=8
    assert n == 0
    outs = [m for m in new if m.get("type") == "function_call_output"]
    assert all(len(m["output"]) > 4000 for m in outs)
    rs = [m for m in new if m.get("type") == "reasoning"]
    assert all(r["summary"][0]["text"].startswith("think") for r in rs)


def test_placeholder_head_cut_reclosed_when_fence_left_open():
    # F2 regression: MICRO_PLACEHOLDER_HEAD (300 chars) truncates the head
    # well before a P1-style placeholder's own closing fence, leaving
    # <untrusted-data ...> unclosed downstream. The compacted result must
    # close the fence itself.
    to.OFFLOAD_DIR_VAR.set("")
    trailer = "[tool output offloaded: chars=9000 path=/DATA/x/call_1.txt]"
    ph = ("<untrusted-data source=\"tool-output-preview\">\n" + "p" * 1500
          + "\n</untrusted-data>\n" + trailer + "\nUse read_file_lines...")
    items = [_u("go"), _fc("c0"), _fo("c0", ph)] + sum(
        [[_fc(f"c{i}"), _fo(f"c{i}", "y" * 10)] for i in range(1, 10)], [])
    new, n = cf.micro_compact(items, keep_recent_results=8, keep_chars=800)
    out = [m for m in new if m.get("type") == "function_call_output"][0]["output"]
    assert n == 1
    assert out.count("<untrusted-data") == 1
    assert out.count("</untrusted-data>") == 1
    assert trailer in out
