"""Reading guides for offloaded tool outputs (offload_summary)."""
import asyncio
import os

import pytest

import offload_summary as osum
import run_context as rc
import tool_output as to


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class _Summ:
    """Stand-in for summarizer.make_summarizer()'s return value."""
    def __init__(self, reply="Key facts:\n- a\nSections:\nL1-3  body\nRead next: L1-3", raise_=False):
        self.reply, self.raise_, self.calls = reply, raise_, []

    async def complete(self, instruction, body, *, max_tokens=1024, timeout=None):
        self.calls.append((instruction, body, max_tokens, timeout))
        if self.raise_:
            raise RuntimeError("boom")
        return self.reply


def _ctx(summ):
    ctx = rc.RunCtx(session_id="s", user_id="u", model_name="m", provider_type="other",
                    window=1000, summarize_fn=summ)
    rc.RUN_CTX_VAR.set(ctx)
    return ctx


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(osum, "ENABLED", True)
    yield
    rc.RUN_CTX_VAR.set(None)


def test_summary_path_is_sidecar_next_to_raw():
    assert osum.summary_path("/x/.tool-outputs/c1.txt") == "/x/.tool-outputs/c1.summary.txt"
    assert osum.summary_path("/x/c1") == "/x/c1.summary.txt"


def test_number_lines_caps_input_and_reports_truncation():
    text = "\n".join(f"line {i}" for i in range(1, 1001))
    body, total, truncated = osum.number_lines(text, max_chars=200)
    assert total == 1000 and truncated
    assert body.startswith("1| line 1\n2| line 2")
    assert len(body) <= 200


def test_build_body_has_tool_args_and_line_count():
    body, total = osum.build_body("a\nb\nc", tool_name="web_fetch", args_hint='{"url": "u"}')
    assert total == 3
    assert body.startswith('Tool: web_fetch\nCall arguments: {"url": "u"}\nOutput: 5 chars, 3 lines')
    assert "1| a\n2| b\n3| c" in body


def test_summarize_uses_run_ctx_summarizer_with_bounded_timeout():
    s = _Summ()
    _ctx(s)
    out = _run(osum.summarize("hello\nworld", tool_name="t", args_hint="{}"))
    assert out.startswith("Key facts:")
    (instr, body, max_tokens, timeout), = s.calls
    assert "READING GUIDE" in instr and "1| hello" in body
    assert timeout == osum.SUMMARY_TIMEOUT and max_tokens == 600


def test_summarize_returns_empty_without_ctx_or_when_disabled(monkeypatch):
    rc.RUN_CTX_VAR.set(None)
    assert _run(osum.summarize("x", tool_name="t")) == ""
    _ctx(_Summ())
    monkeypatch.setattr(osum, "ENABLED", False)
    assert _run(osum.summarize("x", tool_name="t")) == ""


def test_summarize_swallows_errors_and_caps_length():
    _ctx(_Summ(raise_=True))
    assert _run(osum.summarize("x", tool_name="t")) == ""
    _ctx(_Summ(reply="y" * 5000))
    out = _run(osum.summarize("x", tool_name="t"))
    assert len(out) <= osum.MAX_SUMMARY_CHARS + 40 and out.endswith("…(guide truncated)")


def test_attach_replaces_preview_keeps_trailer_and_stores_sidecar(tmp_path):
    _ctx(_Summ())
    text = "x" * 8000
    path = str(tmp_path / "c1.txt")
    (tmp_path / "c1.txt").write_text(text)
    ph = to.make_placeholder(text, tool_name="web_fetch", path=path, chars=8000)
    out = _run(osum.attach(ph, text, tool_name="web_fetch", path=path))
    assert out.startswith('<untrusted-data source="tool-output-summary">')
    assert "tool-output-preview" not in out
    assert f"[tool output offloaded: chars=8000 path={path}]" in out
    assert "reading guide for the offloaded output above: 1 lines" in out
    assert (tmp_path / "c1.summary.txt").read_text().startswith("Key facts:")
    assert osum.SUMMARY_BLOCK_RE.search(out)


def test_attach_falls_back_to_placeholder_when_no_summary(tmp_path):
    _ctx(_Summ(reply=""))
    text = "x" * 8000
    path = str(tmp_path / "c1.txt")
    ph = to.make_placeholder(text, tool_name="t", path=path, chars=8000)
    assert _run(osum.attach(ph, text, tool_name="t", path=path)) is ph
    assert not (tmp_path / "c1.summary.txt").exists()


def test_postprocess_async_attaches_guide_for_offloaded_only(tmp_path):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    _ctx(_Summ())
    small = _run(to.postprocess_async("short", tool_name="t", call_id="c0"))
    assert small == "short"
    big = _run(to.postprocess_async("y" * 7000, tool_name="t", call_id="c1", args_hint="{}"))
    assert big.startswith('<untrusted-data source="tool-output-summary">')
    assert "[tool output offloaded: chars=7000" in big
    assert os.path.isfile(tmp_path / "c1.summary.txt")


def test_postprocess_async_without_ctx_is_plain_placeholder(tmp_path):
    to.OFFLOAD_DIR_VAR.set(str(tmp_path))
    rc.RUN_CTX_VAR.set(None)
    big = _run(to.postprocess_async("y" * 7000, tool_name="t", call_id="c2"))
    assert 'source="tool-output-preview"' in big and "tool-output-summary" not in big
