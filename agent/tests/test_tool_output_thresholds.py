import tool_output as to


def test_retrieval_tools_have_a_higher_threshold():
    assert to.offload_threshold_for("nimoos_search") == to.RETRIEVAL_OFFLOAD_THRESHOLD_CHARS
    assert to.offload_threshold_for("read_file_chunk") == to.RETRIEVAL_OFFLOAD_THRESHOLD_CHARS
    assert to.offload_threshold_for("read_document") == to.RETRIEVAL_OFFLOAD_THRESHOLD_CHARS
    assert to.offload_threshold_for("web_fetch") == to.OFFLOAD_THRESHOLD_CHARS
    assert to.RETRIEVAL_OFFLOAD_THRESHOLD_CHARS > to.OFFLOAD_THRESHOLD_CHARS


def test_search_result_under_retrieval_threshold_is_not_folded(tmp_path):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    text = "x" * (to.OFFLOAD_THRESHOLD_CHARS + 5000)          # over the generic cap …
    assert len(text) < to.RETRIEVAL_OFFLOAD_THRESHOLD_CHARS   # … under the retrieval cap
    assert to.postprocess(text, tool_name="nimoos_search", call_id="c1") is text
    assert not list(tmp_path.iterdir())                       # nothing written


def test_search_result_over_retrieval_threshold_still_folds(tmp_path):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    text = "y" * (to.RETRIEVAL_OFFLOAD_THRESHOLD_CHARS + 1)
    out = to.postprocess(text, tool_name="nimoos_search", call_id="c2")
    assert out is not text and to.TRAILER_RE.search(out)


def test_generic_tool_keeps_the_generic_threshold(tmp_path):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    text = "z" * (to.OFFLOAD_THRESHOLD_CHARS + 1)
    out = to.postprocess(text, tool_name="web_fetch", call_id="c3")
    assert out is not text and to.TRAILER_RE.search(out)
